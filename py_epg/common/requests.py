#!/usr/bin/env python3
"""Module defines request related configurations."""
import time

from urllib3 import util

import requests
from requests import adapters


class ThrottledSession(requests.Session):
    """Session enforcing a minimum delay between requests (per process,
    like the scraper throttle). Used for metadata lookup sessions."""

    def __init__(self, request_delay=0.0):
        super().__init__()
        self._request_delay = request_delay
        self._last_request = 0.0
        self.__attrs__ = list(self.__attrs__) + [
            '_request_delay', '_last_request']

    def __setstate__(self, state):
        super().__setstate__(state)
        # a pickled task copy can't inherit live throttle state - mark
        # the budget as just used so a wave of new tasks can't burst
        if self._request_delay > 0:
            self._last_request = time.monotonic()

    def request(self, method, url, **kwargs):
        if self._request_delay > 0:
            wait = self._request_delay - \
                (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
        return super().request(method, url, **kwargs)


def get_http_session(
        retries=5,
        backoff_factor=20.0,
        status_forcelist=(403, 429, 500, 502, 503, 504),
        session=None,
        proxy=None,
        user_agent=None,
        retry_after_max=180,
        request_delay=0.0,
) -> requests.Session:
    """
    Build request retry policy.
    wait bydefault to 5+ mins in 5 retries unless these settings overriden by client.
    retry_after_max caps server-sent Retry-After waits (urllib3's default
    is 6 hours, which would stall a worker for hours on a rate-limit ban).
    """
    if session is None:
        session = ThrottledSession(request_delay) if request_delay \
            else requests.Session()
    retry = util.Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
        retry_after_max=retry_after_max,
    )
    adapter = adapters.HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    if user_agent:
        session.headers.update({'User-Agent': user_agent})
    if proxy:
        session.proxies.update({'http': proxy, 'https': proxy})
    return session
