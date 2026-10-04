#!/usr/bin/env python3

import pickle
import sqlite3
import time

import pytest

from py_epg.common.cache import Cache, NULL_CACHE


class TestGetSet:
    def test_roundtrip(self, cache):
        cache.set('k', {'a': 1}, 'meta')
        assert cache.get('k') == {'a': 1}

    def test_miss_returns_default(self, cache):
        assert cache.get('nope') is None
        assert cache.get('nope', 'd') == 'd'

    def test_overwrite(self, cache):
        cache.set('k', 1, 'meta')
        cache.set('k', 2, 'meta')
        assert cache.get('k') == 2

    def test_json_values(self, cache):
        for v in ['str', 12, None, [1, 2], {'x': {'y': []}}, True]:
            cache.set('k', v)
            assert cache.get('k') == v


class TestTtl:
    def test_expired_entry_reads_as_miss_and_is_deleted(self, tmp_path):
        c = Cache(path=str(tmp_path / 'c.sqlite'),
                  ttls={'meta': -1})  # already expired
        c.set('k', 'v', 'meta')
        assert c.get('k') is None
        # row was purged on read
        row = c._connect().execute(
            'SELECT 1 FROM cache WHERE key = ?', ('k',)).fetchone()
        assert row is None
        c.close()

    def test_category_ttl_used(self, tmp_path):
        c = Cache(path=str(tmp_path / 'c.sqlite'),
                  ttls={'meta': 5000}, default_ttl=10)
        c.set('a', 1, 'meta')
        c.set('b', 1)  # uncategorised -> default ttl
        exp = dict(c._connect().execute('SELECT key, expires FROM cache'))
        assert exp['a'] > time.time() + 1000
        assert exp['b'] < time.time() + 100
        c.close()

    def test_delete(self, cache):
        cache.set('k', 'v')
        cache.delete('k')
        assert cache.get('k') is None
        cache.delete('k')  # missing key is a no-op

    def test_explicit_ttl_overrides_category(self, cache):
        cache.set('k', 'v', ttl=-1)
        assert cache.get('k') is None
        cache.set('k', 'v', 'meta', ttl=3600)
        assert cache.get('k') == 'v'

    def test_delete_expired(self, tmp_path):
        c = Cache(path=str(tmp_path / 'c.sqlite'), ttls={'meta': -1})
        c.set('old', 1, 'meta')
        c.set('new', 2, 'channel')
        c.delete_expired()
        assert c.get('old') is None
        assert c.get('new') == 2  # channel fell back to default ttl
        c.close()


class TestDisabled:
    def test_get_always_misses(self):
        c = Cache(enabled=False)
        c.set('k', 'v')
        assert c.get('k') is None
        assert NULL_CACHE.get('k') is None


class TestPickling:
    def test_survives_pickling_and_reconnects(self, cache):
        cache.set('k', {'v': 1}, 'meta')
        clone = pickle.loads(pickle.dumps(cache))
        assert clone._conn is None  # connection not carried over
        assert clone.get('k') == {'v': 1}  # lazily reconnects, data shared


class TestProxyStats:
    def test_counters_accumulate(self, cache):
        cache.record_proxy_result('1.2.3.4:8080', ok=False)
        cache.record_proxy_result('1.2.3.4:8080', ok=False)
        cache.record_proxy_result('1.2.3.4:8080', ok=True)
        row = cache._connect().execute(
            'SELECT fails, successes FROM proxy_stats WHERE proxy = ?',
            ('1.2.3.4:8080',)).fetchone()
        assert row == (2, 1)

    def test_never_raises_on_db_error(self, tmp_path, monkeypatch):
        c = Cache(path=str(tmp_path / 'x.sqlite'))
        monkeypatch.setattr(c, '_connect',
                            lambda: (_ for _ in ()).throw(sqlite3.Error()))
        c.record_proxy_result('h:p', ok=True)  # must not raise
        c.close()


class TestProxyDead:
    def test_dead_until_mirrored_and_cleared(self, cache):
        cache.record_proxy_dead('1.2.3.4:8080', 99999.0)
        row = cache._connect().execute(
            'SELECT dead_until FROM proxy_stats WHERE proxy = ?',
            ('1.2.3.4:8080',)).fetchone()
        assert row == (99999.0,)
        cache.record_proxy_dead('1.2.3.4:8080')
        row = cache._connect().execute(
            'SELECT dead_until FROM proxy_stats WHERE proxy = ?',
            ('1.2.3.4:8080',)).fetchone()
        assert row == (None,)

    def test_migration_adds_dead_until_to_old_db(self, tmp_path):
        # create a DB with the pre-dead_until proxy_stats schema
        path = str(tmp_path / 'old.sqlite')
        conn = sqlite3.connect(path)
        conn.execute('''CREATE TABLE proxy_stats (
            proxy TEXT PRIMARY KEY, fails INTEGER NOT NULL DEFAULT 0,
            successes INTEGER NOT NULL DEFAULT 0, last_fail REAL,
            last_success REAL, first_seen REAL NOT NULL,
            last_seen REAL NOT NULL)''')
        conn.execute('INSERT INTO proxy_stats '
                     '(proxy, fails, successes, first_seen, last_seen) '
                     "VALUES ('1.1.1.1:1', 5, 0, 1, 1)")
        conn.commit()
        conn.close()
        c = Cache(path=path)
        c.record_proxy_dead('1.1.1.1:1', 42.0)
        row = c._connect().execute(
            'SELECT fails, dead_until FROM proxy_stats WHERE proxy = ?',
            ('1.1.1.1:1',)).fetchone()
        assert row == (5, 42.0)  # existing stats preserved
        c.close()


class TestIncrement:
    def test_counts_up(self, cache):
        assert cache.increment('n', ttl=60) == 1
        assert cache.increment('n', ttl=60) == 2
        assert cache.increment('n', ttl=60) == 3

    def test_expired_counter_restarts(self, cache):
        cache.increment('n', ttl=-1)
        assert cache.increment('n', ttl=60) == 1

    def test_disabled_returns_zero(self):
        assert Cache(enabled=False).increment('n') == 0
