# Copyright (c) 2010-2011 OpenStack, LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import multiprocessing
import Queue
import datetime
import zlib
import time
from paste.deploy import appconfig
from contextlib import contextmanager
import os
import errno
import fcntl

from eventlet import sleep

from swift.common.memcached import MemcacheRing
from slogging.internal_proxy import InternalProxy
from swift.common.utils import get_logger
from swift.common.exceptions import ChunkReadTimeout, LockTimeout


class BadFileDownload(Exception):
    def __init__(self, status_code=None):
        self.status_code = status_code


class LogProcessorCommon(object):

    def __init__(self, conf, logger, log_route='log-processor'):
        if isinstance(logger, tuple):
            self.logger = get_logger(*logger, log_route=log_route)
        else:
            self.logger = logger
        self.memcache = MemcacheRing([s.strip() for s in
            conf.get('memcache_servers', '').split(',')
            if s.strip()])
        self.conf = conf
        self._internal_proxy = None

    @property
    def internal_proxy(self):
        if self._internal_proxy is None:
            # first look in the conf directly
            proxy_server_conf_loc = self.conf.get('proxy_server_conf')
            if proxy_server_conf_loc is None:
                # then look in a section called log-processor
                stats_conf = self.conf.get('log-processor', {})
                proxy_server_conf_loc = stats_conf.get('proxy_server_conf',
                                                '/etc/swift/proxy-server.conf')
            if proxy_server_conf_loc:
                proxy_server_conf = appconfig(
                                        'config:%s' % proxy_server_conf_loc,
                                        name='proxy-server')
            else:
                proxy_server_conf = None
            self._internal_proxy = InternalProxy(proxy_server_conf,
                                                 self.logger,
                                                 retries=3)
        return self._internal_proxy

    def get_object_data(self, swift_account, container_name, object_name,
                        compressed=False):
        '''reads an object and yields its lines'''
        code, o = self.internal_proxy.get_object(swift_account, container_name,
                                                 object_name)
        if code < 200 or code >= 300:
            raise BadFileDownload(code)
        last_part = ''
        last_compressed_part = ''
        # magic in the following zlib.decompressobj argument is courtesy of
        # Python decompressing gzip chunk-by-chunk
        # http://stackoverflow.com/questions/2423866
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            for chunk in o:
                if compressed:
                    try:
                        chunk = d.decompress(chunk)
                    except zlib.error:
                        self.logger.debug(_('Bad compressed data for %s')
                            % '/'.join((swift_account, container_name,
                                        object_name)))
                        raise BadFileDownload()  # bad compressed data
                parts = chunk.split('\n')
                parts[0] = last_part + parts[0]
                for part in parts[:-1]:
                    yield part
                last_part = parts[-1]
            if last_part:
                yield last_part
        except ChunkReadTimeout:
            raise BadFileDownload()

    def get_container_listing(self, swift_account, container_name,
                              start_date=None, end_date=None,
                              listing_filter=None):
        '''
        Get a container listing, filtered by start_date, end_date, and
        listing_filter. Dates, if given, must be in YYYYMMDDHH format
        '''
        search_key = None
        if start_date is not None:
            try:
                parsed_date = time.strptime(start_date, '%Y%m%d%H')
            except ValueError:
                pass
            else:
                year = '%04d' % parsed_date.tm_year
                month = '%02d' % parsed_date.tm_mon
                day = '%02d' % parsed_date.tm_mday
                hour = '%02d' % parsed_date.tm_hour
                search_key = '/'.join([year, month, day, hour])
        end_key = None
        if end_date is not None:
            try:
                parsed_date = time.strptime(end_date, '%Y%m%d%H')
            except ValueError:
                pass
            else:
                year = '%04d' % parsed_date.tm_year
                month = '%02d' % parsed_date.tm_mon
                day = '%02d' % parsed_date.tm_mday
                # Since the end_marker filters by <, we need to add something
                # to make sure we get all the data under the last hour. Adding
                # one to the hour should be all-inclusive.
                hour = '%02d' % (parsed_date.tm_hour + 1)
                end_key = '/'.join([year, month, day, hour])
        container_listing = self.internal_proxy.get_container_list(
                                    swift_account,
                                    container_name,
                                    marker=search_key,
                                    end_marker=end_key)
        results = []
        if listing_filter is None:
            listing_filter = set()
        for item in container_listing:
            name = item['name']
            if name not in listing_filter:
                results.append(name)
        return results


def multiprocess_collate(processor_klass, processor_args, processor_method,
                         items_to_process, worker_count, logger=None):
    '''
    '''
    results = []
    in_queue = multiprocessing.Queue()
    out_queue = multiprocessing.Queue()
    for _junk in range(worker_count):
        p = multiprocessing.Process(target=collate_worker,
                                    args=(processor_klass,
                                          processor_args,
                                          processor_method,
                                          in_queue,
                                          out_queue))
        p.start()
        results.append(p)
    for x in items_to_process:
        in_queue.put(x)
    for _junk in range(worker_count):
        in_queue.put(None)  # tell the worker to end
    while True:
        try:
            item, data = out_queue.get_nowait()
        except Queue.Empty:
            sleep(.01)
        else:
            if isinstance(data, Exception):
                if logger:
                    logger.exception(data)
            else:
                yield item, data
        if not any(r.is_alive() for r in results) and out_queue.empty():
            # all the workers are done and nothing is in the queue
            break


def collate_worker(processor_klass, processor_args, processor_method, in_queue,
                   out_queue):
    '''worker process for multiprocess_collate'''
    p = processor_klass(*processor_args)
    while True:
        item = in_queue.get()
        if item is None:
            # no more work to process
            break
        try:
            method = getattr(p, processor_method)
        except AttributeError:
            return
        try:
            ret = method(*item)
        except Exception, err:
            ret = err
        out_queue.put((item, ret))
