#!/usr/bin/env python3
"""Persistent, TTL-based JSON cache backed by SQLite.

The cache connection is opened lazily so that Cache objects can be pickled
into multiprocessing workers - each process opens its own connection to the
same database file (SQLite handles concurrent access; WAL mode + busy
timeout reduce lock contention).
"""

import json
import logging
import os
import sqlite3
import threading
import time

log = logging.getLogger(__name__)

_SCHEMA = '''
CREATE TABLE IF NOT EXISTS cache (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL,
    expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS proxy_stats (
    proxy        TEXT PRIMARY KEY,
    fails        INTEGER NOT NULL DEFAULT 0,
    successes    INTEGER NOT NULL DEFAULT 0,
    last_fail    REAL,
    last_success REAL,
    first_seen   REAL NOT NULL,
    last_seen    REAL NOT NULL
)
'''


class Cache:
    """
    A simple persistent key/value cache.

    Values are stored as JSON. `ttl`s are per-category seconds; the
    'default' category is used when no category is given.
    """

    def __init__(self, path='epg_cache.sqlite', ttls=None, enabled=True,
                 default_ttl=86400):
        self._path = path
        self._enabled = enabled
        self._default_ttl = default_ttl
        self._ttls = ttls or {}
        self._conn = None
        # Serialises access to the shared connection - check_same_thread=
        # False permits multi-threaded use but doesn't make it safe.
        self._lock = threading.RLock()

    def _connect(self):
        if self._conn is None:
            os.makedirs(os.path.dirname(os.path.abspath(self._path)),
                        exist_ok=True)
            self._conn = sqlite3.connect(
                self._path, timeout=30, check_same_thread=False)
            self._conn.execute('PRAGMA journal_mode=WAL')
            # Writers come from many worker processes; wait out short
            # locks instead of erroring.
            self._conn.execute('PRAGMA busy_timeout=10000')
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        return self._conn

    def __getstate__(self):
        state = self.__dict__.copy()
        state['_conn'] = None
        state['_lock'] = None
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self._lock = threading.RLock()

    def get(self, key, default=None):
        if not self._enabled:
            return default
        with self._lock:
            row = self._connect().execute(
                'SELECT value, expires FROM cache WHERE key = ?',
                (key,)).fetchone()
            if row is None:
                return default
            if row[1] < time.time():
                self._connect().execute(
                    'DELETE FROM cache WHERE key = ?', (key,))
                self._conn.commit()
                return default
            return json.loads(row[0])

    def set(self, key, value, category=None, ttl=None):
        if not self._enabled:
            return
        if ttl is None:
            ttl = self._ttls.get(category, self._default_ttl) \
                if category else self._default_ttl
        with self._lock:
            self._connect().execute(
                'INSERT OR REPLACE INTO cache (key, value, expires) '
                'VALUES (?, ?, ?)',
                (key, json.dumps(value), time.time() + ttl))
            self._conn.commit()

    def increment(self, key, ttl=None) -> int:
        """
        Atomically increments an integer counter (a single upsert, so
        concurrent workers can't lose updates). An expired row restarts
        at 1. Returns the new value.
        """
        if not self._enabled:
            return 0
        now = time.time()
        expires = now + (ttl if ttl is not None else self._default_ttl)
        with self._lock:
            conn = self._connect()
            conn.execute(
                '''INSERT INTO cache (key, value, expires)
                       VALUES (?, '1', ?)
                   ON CONFLICT(key) DO UPDATE SET
                       value = CASE WHEN expires < ? THEN 1
                               ELSE CAST(value AS INTEGER) + 1 END,
                       expires = ?''',
                (key, expires, now, expires))
            row = conn.execute(
                'SELECT value FROM cache WHERE key = ?', (key,)).fetchone()
            conn.commit()
        return int(row[0]) if row else 0

    def delete(self, key):
        """Removes a key. No-op when disabled or the key is missing."""
        if not self._enabled:
            return
        with self._lock:
            self._connect().execute(
                'DELETE FROM cache WHERE key = ?', (key,))
            self._conn.commit()

    def ttl(self, category):
        return self._ttls.get(category, self._default_ttl)

    def delete_expired(self):
        if not self._enabled:
            return
        with self._lock:
            cur = self._connect().execute(
                'DELETE FROM cache WHERE expires < ?', (time.time(),))
            self._conn.commit()
            if cur.rowcount:
                log.debug(f'Cache: deleted {cur.rowcount} expired entries')

    def record_proxy_result(self, proxy: str, ok: bool):
        """
        Persist a per-proxy success/failure counter (keyed by host:port,
        no credentials). Best-effort - stats never break a request path.
        """
        if not self._enabled:
            return
        now = time.time()
        try:
            with self._lock:
                self._connect().execute(
                    '''INSERT INTO proxy_stats
                           (proxy, fails, successes, last_fail,
                            last_success, first_seen, last_seen)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(proxy) DO UPDATE SET
                           fails = fails + excluded.fails,
                           successes = successes + excluded.successes,
                           last_fail = MAX(COALESCE(last_fail, 0),
                                           COALESCE(excluded.last_fail, 0)),
                           last_success = MAX(COALESCE(last_success, 0),
                                              COALESCE(excluded.last_success, 0)),
                           last_seen = excluded.last_seen''',
                    (proxy, 0 if ok else 1, 1 if ok else 0,
                     None if ok else now, now if ok else None, now, now))
                self._conn.commit()
        except sqlite3.Error as e:
            log.debug(f'Failed to record proxy stat for {proxy}: {e}')

    def close(self):
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


# A disabled cache instance used as a default - get() always misses,
# set() is a no-op.
NULL_CACHE = Cache(enabled=False)
