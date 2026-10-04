#!/usr/bin/env python3
"""Tests for the shared HTTP-session builder and ThrottledSession."""

import pickle
from unittest.mock import MagicMock, patch

import requests

from py_epg.common.requests import ThrottledSession, get_http_session


class TestThrottledSession:
    def test_sleeps_between_requests(self):
        s = ThrottledSession(request_delay=10.0)
        ok = MagicMock(status_code=200)
        with patch.object(requests.Session, 'request', return_value=ok), \
                patch('py_epg.common.requests.time.sleep') as sleep:
            s.request('GET', 'http://x')
            s.request('GET', 'http://y')
        assert sleep.call_count == 1  # first request is immediate
        assert sleep.call_args.args[0] > 9.0

    def test_no_delay_no_sleep(self):
        s = ThrottledSession(request_delay=0.0)
        ok = MagicMock(status_code=200)
        with patch.object(requests.Session, 'request', return_value=ok), \
                patch('py_epg.common.requests.time.sleep') as sleep:
            s.request('GET', 'http://x')
            s.request('GET', 'http://y')
        sleep.assert_not_called()

    def test_pickled_copy_throttles_first_request(self):
        """Task copies must not burst: an unpickled session treats the
        budget as just used, so its first request waits request_delay."""
        s = pickle.loads(pickle.dumps(ThrottledSession(request_delay=10.0)))
        ok = MagicMock(status_code=200)
        with patch.object(requests.Session, 'request', return_value=ok), \
                patch('py_epg.common.requests.time.sleep') as sleep:
            s.request('GET', 'http://x')
        assert sleep.call_count == 1


class TestGetHttpSession:
    def test_request_delay_builds_throttled_session(self):
        s = get_http_session(request_delay=1.0)
        assert isinstance(s, ThrottledSession)
        assert s._request_delay == 1.0

    def test_no_delay_builds_plain_session(self):
        s = get_http_session()
        assert type(s) is requests.Session
