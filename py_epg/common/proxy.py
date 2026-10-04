#!/usr/bin/env python3
"""Rotating proxy pool with circuit breaker + per-request proxy session."""

import logging
import threading
import time
from collections import OrderedDict

import requests
from requests import adapters
from urllib3 import util

log = logging.getLogger(__name__)

KNOWN_SCHEMES = ('http', 'https', 'socks4', 'socks4a', 'socks5', 'socks5h')

# Errors considered the proxy's fault (as opposed to the target site's).
PROXY_FAILURES = (
    requests.exceptions.ProxyError,
    requests.exceptions.ConnectTimeout,
    requests.exceptions.SSLError,
    requests.exceptions.ConnectionError,
)


def normalize_proxy(line: str, default_scheme='http') -> str:
    """
    Normalize a proxy list entry into a full proxy URL.

    Accepts 'scheme://host:port', bare 'host:port' (assumed http) and
    'scheme,host,port' CSV-style entries. Returns None for unparsable lines.
    """
    line = line.strip()
    if not line or line.startswith('#'):
        return None
    if '://' in line:
        scheme = line.split('://', 1)[0].lower()
        return line if scheme in KNOWN_SCHEMES else None
    if ',' in line:
        parts = [p.strip() for p in line.split(',')]
        if len(parts) == 3 and parts[0].lower() in KNOWN_SCHEMES:
            return f'{parts[0].lower()}://{parts[1]}:{parts[2]}'
        return None
    return f'{default_scheme}://{line}'


class ProxyPool:
    """
    A rotating pool of proxies with a circuit breaker.

    Proxies are acquired round-robin. A proxy that fails `max_fails` times in
    a row is skipped for `cooldown` seconds, then retried automatically.
    If `url` is set, the list is (re)fetched every `refresh` seconds, keeping
    failure stats for proxies that are still present.
    """

    def __init__(self, url=None, proxies=(), refresh=300, max_fails=3,
                 cooldown=600, timeout=10, tries=3, allow_direct=True):
        self.url = url
        self.refresh = refresh
        self.max_fails = max_fails
        self.cooldown = cooldown
        self.timeout = timeout
        self.tries = tries
        self.allow_direct = allow_direct
        # proxy url -> {'fails', 'success', 'dead_until', 'rtt'}
        self._proxies = OrderedDict()
        self._rr_index = 0
        self._last_refresh = 0.0
        self._lock = threading.Lock()
        for p in proxies or ():
            self._add(p)

    def _add(self, proxy):
        normalized = normalize_proxy(proxy)
        if normalized and normalized not in self._proxies:
            self._proxies[normalized] = {
                'fails': 0, 'success': 0, 'dead_until': 0.0, 'rtt': 0.0}

    def __getstate__(self):
        state = self.__dict__.copy()
        del state['_lock']
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self._lock = threading.Lock()

    def _refresh_list(self, now):
        if not self.url or now - self._last_refresh < self.refresh:
            return
        self._last_refresh = now
        try:
            resp = requests.get(self.url, timeout=self.timeout)
            resp.raise_for_status()
        except requests.RequestException as e:
            log.warning(f'Failed to refresh proxy list from {self.url}: {e}')
            return
        before = len(self._proxies)
        for line in resp.text.splitlines():
            self._add(line)
        log.info(
            f'Proxy list refreshed: {len(self._proxies)} proxies '
            f'({len(self._proxies) - before} new) from {self.url}')

    def acquire(self) -> str:
        """Returns the next healthy proxy URL, or None if the pool is empty."""
        now = time.monotonic()
        with self._lock:
            self._refresh_list(now)
            if not self._proxies:
                return None
            keys = list(self._proxies.keys())
            for i in range(len(keys)):
                idx = (self._rr_index + i) % len(keys)
                if self._proxies[keys[idx]]['dead_until'] <= now:
                    self._rr_index = (idx + 1) % len(keys)
                    return keys[idx]
            return None

    def report_success(self, proxy, rtt=None):
        with self._lock:
            stats = self._proxies.get(proxy)
            if stats is None:
                return
            stats['fails'] = 0
            stats['dead_until'] = 0.0
            stats['success'] += 1
            if rtt is not None:
                stats['rtt'] = rtt if stats['rtt'] == 0 else \
                    stats['rtt'] * 0.8 + rtt * 0.2

    def report_failure(self, proxy):
        now = time.monotonic()
        with self._lock:
            stats = self._proxies.get(proxy)
            if stats is None:
                return
            stats['fails'] += 1
            if stats['fails'] >= self.max_fails:
                stats['dead_until'] = now + self.cooldown
                stats['fails'] = 0
                log.debug(
                    f'Proxy {proxy} marked dead for {self.cooldown}s '
                    f'after {self.max_fails} consecutive failures')

    def stats(self):
        now = time.monotonic()
        alive = sum(1 for s in self._proxies.values()
                    if s['dead_until'] <= now)
        return {'total': len(self._proxies), 'alive': alive}


class RotatingProxySession(requests.Session):
    """
    A requests.Session that routes every request through a ProxyPool.

    Each request goes through a different healthy proxy. On proxy-level
    failures (connect, timeout, TLS, refused) and on retryable HTTP
    statuses (`status_forcelist`, e.g. 403/429 rate-limiting), the proxy
    is reported to the pool and the request is retried through the next
    proxy (up to `tries`). If all tries are exhausted, the request either
    goes out directly (`allow_direct`) or raises/returns the last result.
    """

    def __init__(self, pool: ProxyPool,
                 status_forcelist=(403, 429, 500, 502, 503, 504),
                 user_agent=None):
        super().__init__()
        self._pool = pool
        self._status_forcelist = set(status_forcelist)
        # All retries are handled at the pool level: a failing or
        # rate-limited proxy is swapped for the next one, which is far
        # better than sleeping and retrying through the same (banned) IP.
        retry = util.Retry(total=0)
        adapter = adapters.HTTPAdapter(max_retries=retry)
        self.mount('http://', adapter)
        self.mount('https://', adapter)
        if user_agent:
            self.headers.update({'User-Agent': user_agent})
        # requests.Session pickling only preserves attributes listed in
        # __attrs__ - add ours so the session survives being pickled into
        # multiprocessing workers.
        self.__attrs__ = list(self.__attrs__) + [
            '_pool', '_status_forcelist']

    def request(self, method, url, **kwargs):
        kwargs.setdefault('timeout', self._pool.timeout)
        last_error = None
        last_resp = None
        for _ in range(max(1, self._pool.tries)):
            proxy = self._pool.acquire()
            if proxy is None:
                break
            kwargs['proxies'] = {'http': proxy, 'https': proxy}
            try:
                start = time.monotonic()
                resp = super().request(method, url, **kwargs)
                if resp.status_code in self._status_forcelist:
                    # e.g. 403/429: the target site is rate-limiting this
                    # proxy's IP - mark it dead and try the next one.
                    log.debug(
                        f'Proxy {proxy} got HTTP {resp.status_code} for {url}')
                    self._pool.report_failure(proxy)
                    resp.close()
                    last_resp = resp
                    continue
                self._pool.report_success(proxy, time.monotonic() - start)
                return resp
            except PROXY_FAILURES as e:
                log.debug(f'Proxy {proxy} failed for {url}: {e}')
                self._pool.report_failure(proxy)
                last_error = e
        if self._pool.allow_direct:
            if last_error is not None or last_resp is not None:
                log.warning(
                    'All proxies failed, falling back to direct connection '
                    f'for {url}')
            kwargs.pop('proxies', None)
            return super().request(method, url, **kwargs)
        if last_error is not None:
            raise last_error
        if last_resp is not None:
            return last_resp
        raise requests.exceptions.ConnectionError(
            'No healthy proxies available in the pool')


def get_proxy_session(pool: ProxyPool,
                      user_agent=None) -> RotatingProxySession:
    return RotatingProxySession(pool, user_agent=user_agent)
