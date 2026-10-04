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


def pickle_roundtrip(obj):
    import pickle
    return pickle.loads(pickle.dumps(obj))
