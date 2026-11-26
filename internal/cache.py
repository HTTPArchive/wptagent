# Copyright 2019 WebPageTest LLC.
# Copyright 2017 Google Inc.
# Copyright 2020 Catchpoint Systems Inc.
# Use of this source code is governed by the Polyform Shield 1.0.0 license that can be
# found in the LICENSE.md file.
"""Process-lifetime thread-safe LRU caches for common 3P analysis and static agent files"""
from collections import OrderedDict
import hashlib
import io
import logging
import os
import threading
try:
    import ujson as json
except BaseException:
    import json

MISSING = object()


class LRUCache(object):
    """Thread-safe bounded LRU cache with optional JSON cloning for mutable values"""
    def __init__(self, max_size=1000, clone_json=False):
        self.max_size = max_size
        self.clone_json = clone_json
        self._data = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key, default=MISSING):
        """Fetch a value by key, updating LRU order"""
        with self._lock:
            if key not in self._data:
                self.misses += 1
                return default
            self._data.move_to_end(key)
            val = self._data[key]
            self.hits += 1
        if self.clone_json and val is not None:
            return json.loads(val)
        return val

    def set(self, key, value):
        """Store a value by key, evicting the least-recently-used entry if full"""
        stored = json.dumps(value) if (self.clone_json and value is not None) else value
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
            self._data[key] = stored
            while len(self._data) > self.max_size:
                self._data.popitem(last=False)

    def stats(self):
        """Return size, hit, and miss counts"""
        with self._lock:
            return {'size': len(self._data), 'hits': self.hits, 'misses': self.misses}


class AgentCache(object):
    """Process-lifetime caches shared across jobs on a single agent instance"""
    def __init__(self):
        self.cdn_dns = LRUCache(max_size=10000, clone_json=False)
        self.images = LRUCache(max_size=5000, clone_json=True)
        self.progressive = LRUCache(max_size=5000, clone_json=True)
        self.fonts = LRUCache(max_size=2000, clone_json=True)
        self.wasm = LRUCache(max_size=500, clone_json=True)
        self.gzip = LRUCache(max_size=2000, clone_json=False)
        self._files = {}
        self._files_lock = threading.Lock()

    @staticmethod
    def hash_file(path):
        """Compute SHA-256 hex digest of a file on disk"""
        if not path or not os.path.isfile(path):
            return None
        try:
            digest = hashlib.sha256()
            with open(path, 'rb') as f_in:
                while True:
                    chunk = f_in.read(65536)
                    if not chunk:
                        break
                    digest.update(chunk)
            return digest.hexdigest()
        except Exception:
            return None

    def get_file_value(self, key, mtime, loader):
        """Cache a derived value for a static file keyed by (key, mtime)"""
        with self._files_lock:
            cached = self._files.get(key)
            if cached is not None and cached[0] == mtime:
                return cached[1]
        value = loader()
        if value is not None:
            with self._files_lock:
                self._files[key] = (mtime, value)
        return value

    def read_text(self, path, encoding='utf-8'):
        """Read and cache a static text file keyed by (abspath, mtime)"""
        abs_path = os.path.abspath(path)
        mtime = os.path.getmtime(abs_path)
        def _load():
            with io.open(abs_path, 'r', encoding=encoding) as f_in:
                return f_in.read()
        return self.get_file_value(('text', abs_path), mtime, _load)

    def read_json(self, path, encoding='utf-8'):
        """Read, parse, and cache a static JSON file keyed by (abspath, mtime)"""
        abs_path = os.path.abspath(path)
        mtime = os.path.getmtime(abs_path)
        def _load():
            with io.open(abs_path, 'r', encoding=encoding) as f_in:
                return json.load(f_in)
        return self.get_file_value(('json', abs_path), mtime, _load)

    def log_stats(self):
        """Log a compact debug summary of LRU cache hit rates"""
        parts = []
        for name, cache in (('cdn_dns', self.cdn_dns),
                            ('images', self.images),
                            ('progressive', self.progressive),
                            ('fonts', self.fonts),
                            ('wasm', self.wasm),
                            ('gzip', self.gzip)):
            st = cache.stats()
            total = st['hits'] + st['misses']
            if total:
                parts.append('{0}={1}/{2}({3})'.format(name, st['hits'], total, st['size']))
        if parts:
            logging.debug('Agent cache stats (hits/lookups(size)): %s', ' '.join(parts))
