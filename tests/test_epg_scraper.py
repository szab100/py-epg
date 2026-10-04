#!/usr/bin/env python3
"""Tests for EpgScraper base: session selection, throttling, today()."""

import time
from datetime import date
from unittest.mock import MagicMock

from py_epg.common.epg_scraper import EpgScraper
from py_epg.common.proxy import ProxyPool, RotatingProxySession
from py_epg.scrapers.m_musor_tv import MusorTvMobile


def make_scraper(**kwargs):
    kwargs.setdefault('user_agent', 'test')
    kwargs.setdefault('request_delay', 0)
    return MusorTvMobile(**kwargs)


class TestSessionSelection:
    def test_proxy_pool_uses_rotating_session(self):
        pool = ProxyPool()
        s = make_scraper(proxy=pool)
        assert isinstance(s._http, RotatingProxySession)
        assert s._timeout == pool.timeout

    def test_static_proxy_uses_regular_session(self):
        s = make_scraper(proxy='http://h:1')
        assert not isinstance(s._http, RotatingProxySession)
        assert s._timeout == 60


class TestThrottle:
    def test_sleeps_until_delay_elapsed(self, monkeypatch):
        s = make_scraper(request_delay=5.0)
        s._last_request = time.monotonic()
        sleep = MagicMock()
        monkeypatch.setattr(time, 'sleep', sleep)
        s._throttle()
        assert sleep.call_count == 1
        assert 0 < sleep.call_args[0][0] <= 5.0

    def test_no_sleep_when_delay_zero(self, monkeypatch):
        s = make_scraper(request_delay=0)
        sleep = MagicMock()
        monkeypatch.setattr(time, 'sleep', sleep)
        s._throttle()
        sleep.assert_not_called()

    def test_no_sleep_when_budget_spent(self, monkeypatch):
        s = make_scraper(request_delay=5.0)
        s._last_request = 0.0          # long ago -> no wait needed
        sleep = MagicMock()
        monkeypatch.setattr(time, 'sleep', sleep)
        s._throttle()
        sleep.assert_not_called()
        assert s._last_request > 0.0   # budget now marked used


class TestToday:
    def test_base_returns_local_date(self):
        # all concrete scrapers override today(); exercise the default
        assert EpgScraper.today(make_scraper()) == date.today()
