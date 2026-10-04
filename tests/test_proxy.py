#!/usr/bin/env python3
"""Tests for the proxy pool: URL normalization, credential handling,
round-robin acquisition and the circuit breaker."""

import time
from unittest.mock import MagicMock, patch

import pytest
import requests

from py_epg.common.proxy import (ProxyPool, RotatingProxySession,
                                 _display, normalize_proxy)


class TestNormalizeProxy:
    @pytest.mark.parametrize('line,expected', [
        ('1.2.3.4:8080', 'http://1.2.3.4:8080'),
        ('http://1.2.3.4:8080', 'http://1.2.3.4:8080'),
        ('socks5://1.2.3.4:1080', 'socks5://1.2.3.4:1080'),
        ('user:pass@1.2.3.4:8080', 'http://user:pass@1.2.3.4:8080'),
        ('1.2.3.4:8080:user:pass', 'http://user:pass@1.2.3.4:8080'),
        # password containing colons
        ('1.2.3.4:8080:u:p:a:s:s', 'http://u:p%3Aa%3As%3As@1.2.3.4:8080'),
        # CSV forms
        ('https,1.2.3.4,8443', 'https://1.2.3.4:8443'),
        ('socks5,1.2.3.4,1080,u,p', 'socks5://u:p@1.2.3.4:1080'),
        ('1.2.3.4,8080,u,p', 'http://u:p@1.2.3.4:8080'),
    ])
    def test_valid(self, line, expected):
        assert normalize_proxy(line) == expected

    @pytest.mark.parametrize('line', [
        '', '# comment', 'ftp://1.2.3.4:21', '1.2.3.4,8080',
    ])
    def test_invalid(self, line):
        assert normalize_proxy(line) is None

    def test_bare_hostname_gets_default_scheme(self):
        assert normalize_proxy('proxy.local') == 'http://proxy.local'


class TestDisplay:
    def test_strips_credentials(self):
        assert _display('http://u:p@1.2.3.4:8080') == '1.2.3.4:8080'

    def test_bare_host_port(self):
        assert _display('1.2.3.4:8080') == '1.2.3.4:8080'


class TestProxyPool:
    def test_round_robin(self):
        pool = ProxyPool(proxies=['1.1.1.1:1', '2.2.2.2:2', '3.3.3.3:3'])
        got = [pool.acquire() for _ in range(4)]
        assert got[:3] == ['http://1.1.1.1:1', 'http://2.2.2.2:2',
                           'http://3.3.3.3:3']
        assert got[3] == got[0]  # wraps around

    def test_empty_pool(self):
        assert ProxyPool().acquire() is None

    def test_benched_after_max_fails(self):
        pool = ProxyPool(proxies=['1.1.1.1:1', '2.2.2.2:2'],
                         max_fails=2, cooldown=600)
        bad = pool.acquire()
        pool.report_failure(bad)
        other = pool.acquire()      # round-robin moves on anyway
        assert other != bad
        pool.report_failure(bad)    # fails=2 -> benched
        assert pool.acquire() == other
        assert pool.acquire() == other
        assert pool.stats() == {'total': 2, 'alive': 1}

    def test_all_benched_acquire_returns_none(self):
        pool = ProxyPool(proxies=['1.1.1.1:1'], max_fails=1)
        bad = pool.acquire()
        pool.report_failure(bad)
        assert pool.acquire() is None

    def test_success_resets_fail_count(self):
        pool = ProxyPool(proxies=['1.1.1.1:1', '2.2.2.2:2'],
                         max_fails=2)
        p1 = pool.acquire()
        pool.report_failure(p1)
        pool.report_success(p1)
        pool.report_failure(p1)   # 1 fail again, not benched
        got = {pool.acquire() for _ in range(4)}
        assert len(got) == 2      # both still rotate

    def test_benched_proxy_recovers_after_cooldown(self):
        pool = ProxyPool(proxies=['1.1.1.1:1'], max_fails=1,
                         cooldown=-1)  # already in the past
        bad = pool.acquire()
        pool.report_failure(bad)
        assert pool.acquire() == bad  # cooldown already elapsed

    def test_stats_recorded_to_db(self, cache):
        pool = ProxyPool(proxies=['http://u:p@1.2.3.4:8080'],
                         stats_db=cache)
        pool.report_failure('http://u:p@1.2.3.4:8080')
        row = cache._connect().execute(
            'SELECT fails FROM proxy_stats WHERE proxy = ?',
            ('1.2.3.4:8080',)).fetchone()
        assert row == (1,)  # stored by host:port - never credentials

    def test_picklable(self):
        pool = ProxyPool(proxies=['1.1.1.1:1'])
        clone = pickle_roundtrip(pool)
        assert clone.acquire() == 'http://1.1.1.1:1'


class TestRotatingProxySession:
    def _session(self, **pool_kw):
        pool = ProxyPool(**pool_kw)
        return RotatingProxySession(pool)

    def test_request_rotates_and_reports_success(self):
        s = self._session(proxies=['1.1.1.1:1', '2.2.2.2:2'])
        ok = MagicMock(status_code=200)
        with patch.object(requests.Session, 'request',
                          return_value=ok) as sup:
            assert s.request('GET', 'http://x') is ok
            proxies_used = sup.call_args.kwargs['proxies']['http']
        assert s._pool._proxies[proxies_used]['success'] == 1

    def test_forcelisted_status_benches_proxy_and_retries(self):
        s = self._session(proxies=['1.1.1.1:1', '2.2.2.2:2'],
                          max_fails=1)
        banned = MagicMock(status_code=403)
        ok = MagicMock(status_code=200)
        with patch.object(requests.Session, 'request',
                          side_effect=[banned, ok]) as sup:
            assert s.request('GET', 'http://x') is ok
            first_proxy = sup.call_args_list[0].kwargs['proxies']['http']
            second_proxy = sup.call_args_list[1].kwargs['proxies']['http']
        assert first_proxy != second_proxy

    def test_proxy_error_tries_next(self):
        s = self._session(proxies=['1.1.1.1:1', '2.2.2.2:2'],
                          max_fails=1)
        ok = MagicMock(status_code=200)
        with patch.object(requests.Session, 'request',
                          side_effect=[requests.exceptions.ProxyError(),
                                       ok]) as sup:
            assert s.request('GET', 'http://x') is ok
            assert sup.call_count == 2

    def test_all_dead_falls_back_to_direct(self):
        s = self._session(proxies=['1.1.1.1:1'], max_fails=1,
                          allow_direct=True, tries=2)
        ok = MagicMock(status_code=200)
        with patch.object(requests.Session, 'request',
                          side_effect=[requests.exceptions.ProxyError(),
                                       ok]) as sup:
            assert s.request('GET', 'http://x') is ok
            # final call went out without proxies
            assert 'proxies' not in sup.call_args.kwargs

    def test_all_dead_no_direct_raises_last_error(self):
        s = self._session(proxies=['1.1.1.1:1'], max_fails=1,
                          allow_direct=False, tries=1)
        err = requests.exceptions.ProxyError('dead')
        with patch.object(requests.Session, 'request',
                          side_effect=err):
            with pytest.raises(requests.exceptions.ProxyError):
                s.request('GET', 'http://x')

    def test_no_proxies_no_direct_raises(self):
        s = self._session(proxies=[], allow_direct=False)
        with pytest.raises(requests.exceptions.ConnectionError):
            s.request('GET', 'http://x')

    def test_read_timeout_tries_next(self):
        """A stalling proxy (connected, never responds) must retry
        through the next proxy - it is not a target-site error."""
        s = self._session(proxies=['1.1.1.1:1', '2.2.2.2:2'],
                          max_fails=1)
        ok = MagicMock(status_code=200)
        with patch.object(requests.Session, 'request',
                          side_effect=[requests.exceptions.ReadTimeout(),
                                       ok]) as sup:
            assert s.request('GET', 'http://x') is ok
            assert sup.call_count == 2

    def test_request_delay_throttles(self):
        """request_delay sleeps between requests, like the scraper
        throttle - used for metadata lookup sessions."""
        pool = ProxyPool(proxies=['1.1.1.1:1'])
        s = RotatingProxySession(pool, request_delay=10.0)
        ok = MagicMock(status_code=200)
        with patch.object(requests.Session, 'request', return_value=ok), \
                patch('py_epg.common.proxy.time.sleep') as sleep:
            s.request('GET', 'http://x')
            s.request('GET', 'http://y')
            assert s.request('GET', 'http://z') is ok
        # first request is immediate, the next two wait ~10s
        assert sleep.call_count == 2
        assert sleep.call_args.args[0] > 9.0


class TestSharedBenchState:
    """Bench markers are shared through the stats DB so workers (each
    holding their own pickled pool copy) don't re-discover the same
    dead proxies."""

    def test_bench_shared_across_pool_copies(self, cache):
        a = ProxyPool(proxies=['1.1.1.1:1'], max_fails=1, stats_db=cache)
        b = ProxyPool(proxies=['1.1.1.1:1'], max_fails=1, stats_db=cache)
        bad = a.acquire()
        a.report_failure(bad)
        assert b.acquire() is None

    def test_success_unbenches_for_everyone(self, cache):
        a = ProxyPool(proxies=['1.1.1.1:1'], max_fails=1, stats_db=cache)
        b = ProxyPool(proxies=['1.1.1.1:1'], max_fails=1, stats_db=cache)
        bad = a.acquire()
        a.report_failure(bad)
        assert b.acquire() is None
        b.report_success(bad)
        assert b.acquire() == 'http://1.1.1.1:1'

    def test_shared_marker_expires_with_cooldown(self, cache):
        pool = ProxyPool(proxies=['1.1.1.1:1'], max_fails=1,
                         cooldown=-1, stats_db=cache)
        bad = pool.acquire()
        pool.report_failure(bad)
        assert pool.acquire() == bad

    def test_unshared_pools_behave_as_before(self):
        a = ProxyPool(proxies=['1.1.1.1:1'], max_fails=1)
        b = ProxyPool(proxies=['1.1.1.1:1'], max_fails=1)
        bad = a.acquire()
        a.report_failure(bad)
        assert b.acquire() == 'http://1.1.1.1:1'  # B doesn't see it


class TestSharedFailStreak:
    """The consecutive-fail counter is shared through the stats DB - each
    task gets a pickled pool copy with a fresh local counter, so without
    sharing a proxy failing <max_fails per task would never bench."""

    def test_streak_shared_across_pool_copies(self, cache):
        a = ProxyPool(proxies=['1.1.1.1:1', '2.2.2.2:2'], max_fails=3,
                      stats_db=cache)
        bad = 'http://1.1.1.1:1'
        # two separate copies each see < max_fails failures
        a.report_failure(bad)
        b = ProxyPool(proxies=['1.1.1.1:1', '2.2.2.2:2'], max_fails=3,
                      stats_db=cache)
        b.report_failure(bad)
        c = ProxyPool(proxies=['1.1.1.1:1', '2.2.2.2:2'], max_fails=3,
                      stats_db=cache)
        c.report_failure(bad)  # shared streak hits 3 -> benched
        d = ProxyPool(proxies=['1.1.1.1:1', '2.2.2.2:2'], max_fails=3,
                      stats_db=cache)
        assert d.acquire() == 'http://2.2.2.2:2'
        assert d.acquire() == 'http://2.2.2.2:2'

    def test_success_resets_shared_streak(self, cache):
        pool = ProxyPool(proxies=['1.1.1.1:1', '2.2.2.2:2'], max_fails=2,
                         stats_db=cache)
        bad = 'http://1.1.1.1:1'
        pool.report_failure(bad)
        pool.report_success(bad)
        pool.report_failure(bad)  # streak is 1 again, not 3
        other = ProxyPool(proxies=['1.1.1.1:1', '2.2.2.2:2'], max_fails=2,
                          stats_db=cache)
        got = {other.acquire() for _ in range(4)}
        assert len(got) == 2  # still in rotation

    def test_streak_expires_with_cooldown_ttl(self, cache):
        pool = ProxyPool(proxies=['1.1.1.1:1', '2.2.2.2:2'], max_fails=2,
                         cooldown=-1, stats_db=cache)
        bad = 'http://1.1.1.1:1'
        pool.report_failure(bad)      # streak 1, but instantly expired
        other = ProxyPool(proxies=['1.1.1.1:1', '2.2.2.2:2'], max_fails=2,
                          cooldown=-1, stats_db=cache)
        other.report_failure(bad)     # stale streak restarted -> 1, not 2
        third = ProxyPool(proxies=['1.1.1.1:1', '2.2.2.2:2'], max_fails=2,
                        stats_db=cache)
        got = {third.acquire() for _ in range(4)}
        assert len(got) == 2


class TestSharedRotation:
    """The round-robin offset is shared so pickled task copies don't all
    start at index 0 and hammer the same first proxy."""

    def test_copies_get_distinct_proxies(self, cache):
        mk = lambda: ProxyPool(
            proxies=['1.1.1.1:1', '2.2.2.2:2', '3.3.3.3:3'],
            stats_db=cache)
        got = {mk().acquire(), mk().acquire(), mk().acquire()}
        assert len(got) == 3

    def test_per_proxy_delay_skips_recently_used(self, cache):
        kw = dict(per_proxy_delay=600, stats_db=cache)
        a = ProxyPool(proxies=['1.1.1.1:1', '2.2.2.2:2'], **kw)
        first = a.acquire()
        b = ProxyPool(proxies=['1.1.1.1:1', '2.2.2.2:2'], **kw)
        assert b.acquire() != first

    def test_per_proxy_delay_zero_unchanged(self, cache):
        kw = dict(per_proxy_delay=0.0, stats_db=cache)
        ProxyPool(proxies=['1.1.1.1:1', '2.2.2.2:2'], **kw).acquire()
        b = ProxyPool(proxies=['1.1.1.1:1', '2.2.2.2:2'], **kw)
        assert b.acquire() in ('http://1.1.1.1:1', 'http://2.2.2.2:2')

    def test_pickled_copy_gets_random_offset(self):
        pool = ProxyPool(proxies=['1.1.1.1:1', '2.2.2.2:2', '3.3.3.3:3'])
        # many copies - the first-acquire should not be deterministic
        starts = {pickle_roundtrip(pool)._rr_index for _ in range(20)}
        assert len(starts) > 1


class TestSharedRefreshState:
    """The list-download timestamp/list is shared so one refresh per
    interval happens globally, not once per pickled pool copy."""

    def _resp(self, text):
        r = MagicMock()
        r.text = text
        r.raise_for_status.return_value = None
        return r

    def test_second_pool_copy_adopts_list_without_refetch(self, cache):
        with patch('py_epg.common.proxy.requests.get',
                   return_value=self._resp('1.1.1.1:1\n2.2.2.2:2')) as g:
            a = ProxyPool(url='http://list', refresh=300, stats_db=cache)
            a.acquire()
            # fresh copy = what a worker task sees after pickling
            b = ProxyPool(url='http://list', refresh=300, stats_db=cache)
            assert b.acquire() in ('http://1.1.1.1:1', 'http://2.2.2.2:2')
            assert g.call_count == 1

    def test_failed_refresh_marks_attempt(self, cache):
        with patch('py_epg.common.proxy.requests.get',
                   side_effect=requests.RequestException('429')) as g:
            a = ProxyPool(url='http://list', refresh=300, stats_db=cache)
            a.acquire()
            b = ProxyPool(url='http://list', refresh=300, stats_db=cache)
            b.acquire()
            assert g.call_count == 1

    def test_stale_list_proxies_removed(self, cache):
        with patch('py_epg.common.proxy.requests.get') as g:
            g.side_effect = [self._resp('1.1.1.1:1'),
                             self._resp('9.9.9.9:9')]
            pool = ProxyPool(url='http://list', refresh=-1,
                             stats_db=cache)
            pool.acquire()
            # expire the shared refresh claim so the next acquire may
            # download again (refresh=-1 only bypasses the local gate)
            cache.delete('proxy:list:claim')
            pool.acquire()
            assert list(pool._proxies) == ['http://9.9.9.9:9']

    def test_static_proxies_survive_refresh(self, cache):
        with patch('py_epg.common.proxy.requests.get',
                   return_value=self._resp('9.9.9.9:9')):
            pool = ProxyPool(url='http://list', proxies=['5.5.5.5:5'],
                             stats_db=cache)
            pool.acquire()
            assert set(pool._proxies) == {'http://5.5.5.5:5',
                                          'http://9.9.9.9:9'}

    def test_no_stats_db_falls_back_to_local_timing(self):
        with patch('py_epg.common.proxy.requests.get',
                   return_value=self._resp('1.1.1.1:1')) as g:
            a = ProxyPool(url='http://list', refresh=300)
            a.acquire()
            a.acquire()
            assert g.call_count == 1  # local _last_refresh still gates


def pickle_roundtrip(obj):
    import pickle
    return pickle.loads(pickle.dumps(obj))
