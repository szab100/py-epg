#!/usr/bin/env python3
"""Rotating proxy pool with circuit breaker + per-request proxy session."""

import logging
import random
import threading
import time
from collections import OrderedDict

import requests
from requests import adapters
from urllib3 import util
from urllib.parse import quote

log = logging.getLogger(__name__)

KNOWN_SCHEMES = ('http', 'https', 'socks4', 'socks4a', 'socks5', 'socks5h')

# Errors considered the proxy's fault (as opposed to the target site's).
PROXY_FAILURES = (
    requests.exceptions.ProxyError,
    requests.exceptions.Timeout,  # covers ConnectTimeout + ReadTimeout
    requests.exceptions.SSLError,
    requests.exceptions.ConnectionError,
)


def _display(proxy: str) -> str:
    """host:port for logging - never log embedded credentials."""
    host = proxy.rsplit('@', 1)[-1]  # strip user:pass@
    host = host.split('://', 1)[-1]  # strip scheme if somehow attached
    return host


def normalize_proxy(line: str, default_scheme='http') -> str:
    """
    Normalize a proxy list entry into a full proxy URL.

    Accepts 'scheme://[user:pass@]host:port', bare 'host:port' (assumed
    http), authenticated 'host:port:user:pass' and 'user:pass@host:port',
    plus CSV-style 'scheme,host,port[,user,pass]' and
    'host,port,user,pass' entries. Returns None for unparsable lines.
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
        if len(parts) == 5 and parts[0].lower() in KNOWN_SCHEMES:
            return (f'{parts[0].lower()}://{quote(parts[3])}:'
                    f'{quote(parts[4])}@{parts[1]}:{parts[2]}')
        if len(parts) == 4:
            return (f'{default_scheme}://{quote(parts[2])}:'
                    f'{quote(parts[3])}@{parts[0]}:{parts[1]}')
        return None
    if '@' in line:
        # user:pass@host:port
        return f'{default_scheme}://{line}'
    parts = line.split(':')
    if len(parts) >= 4 and parts[1].isdigit():
        # host:port:user:pass (password may itself contain colons)
        host, port, user = parts[0], parts[1], parts[2]
        pwd = ':'.join(parts[3:])
        return f'{default_scheme}://{quote(user)}:{quote(pwd)}@{host}:{port}'
    return f'{default_scheme}://{line}'


class ProxyPool:
    """
    A rotating pool of proxies with a circuit breaker.

    Proxies are acquired round-robin. A proxy that fails `max_fails` times in
    a row is skipped for `cooldown` seconds, then retried automatically.
    If `url` is set, the list is (re)fetched every `refresh` seconds, keeping
    failure stats for proxies that are still present.

    Pool copies get pickled into every worker/task, so bench state and
    list-refresh timing are shared through the stats DB - otherwise
    each task would independently re-discover dead proxies and
    re-download the list.
    """

    def __init__(self, url=None, proxies=(), refresh=300, max_fails=3,
                 cooldown=600, timeout=10, tries=3, allow_direct=True,
                 stats_db=None, per_proxy_delay=0.0):
        self.url = url
        self.refresh = refresh
        self.max_fails = max_fails
        self.cooldown = cooldown
        self.timeout = timeout
        self.tries = tries
        self.allow_direct = allow_direct
        # optional minimum seconds between uses of the same proxy IP -
        # shared across all workers via the stats DB
        self._per_proxy_delay = per_proxy_delay
        # Optional Cache instance for persistent per-proxy stats (the
        # proxy_stats table - cumulative across runs and processes).
        self._stats_db = stats_db
        # proxy url -> {'fails', 'success', 'dead_until', 'rtt'}
        self._proxies = OrderedDict()
        self._rr_index = 0
        self._last_refresh = 0.0
        self._lock = threading.Lock()
        for p in proxies or ():
            self._add(p, static=True)

    def _add(self, proxy, static=False):
        normalized = normalize_proxy(proxy)
        if normalized and normalized not in self._proxies:
            self._proxies[normalized] = {
                'fails': 0, 'success': 0, 'dead_until': 0.0, 'rtt': 0.0,
                'static': static}

    def __getstate__(self):
        state = self.__dict__.copy()
        del state['_lock']
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self._lock = threading.Lock()
        # the parent's pool never acquires, so every pickled task copy
        # would start round-robin at index 0 and funnel first requests
        # into the same proxy - randomize the offset instead
        if self._proxies:
            self._rr_index = random.randrange(len(self._proxies))

    def _shared_state(self) -> dict:
        """Last list-download attempt/list shared via the stats DB -
        pool copies pickled into workers/tasks would otherwise each
        fire their own refresh on first acquire."""
        if self._stats_db is None:
            return {}
        try:
            return self._stats_db.get('proxy:list:state') or {}
        except Exception as e:
            log.debug(f'Failed to read shared proxy state: {e}')
            return {}

    def _write_state(self, state):
        if self._stats_db is not None:
            try:
                self._stats_db.set('proxy:list:state', state, 'listing')
            except Exception as e:
                log.debug(f'Failed to write shared proxy state: {e}')

    def refresh_list(self):
        """Refresh the list now (subject to the shared claim) - call in
        the parent before workers spawn so pickled copies inherit a
        populated list and skip their own refresh."""
        if not self.url:
            return
        now = time.monotonic()
        if now - self._last_refresh < self.refresh:
            return
        self._last_refresh = now
        wall = time.time()
        if self._stats_db is not None and not self._stats_db.claim(
                'proxy:list:claim', max(1, int(self.refresh))):
            # another worker owns the refresh for this window - adopt
            # the shared list only if this copy has none of its own.
            # The claim also suppresses retry storms when the owner's
            # download itself fails
            if not self._proxies:
                for line in self._shared_state().get('lines') or ():
                    self._add(line)
            return
        try:
            resp = requests.get(self.url, timeout=self.timeout)
            resp.raise_for_status()
            lines = resp.text.splitlines()
        except requests.RequestException as e:
            log.warning(f'Failed to refresh proxy list from {self.url}: {e}')
            return
        before = len(self._proxies)
        fresh = set()
        for line in lines:
            normalized = normalize_proxy(line)
            if normalized:
                fresh.add(normalized)
                self._add(normalized)
        # drop list proxies that disappeared (statically configured
        # ones always stay)
        if fresh:
            for p in list(self._proxies):
                if p not in fresh and not self._proxies[p]['static']:
                    del self._proxies[p]
        log.info(
            f'Proxy list refreshed: {len(self._proxies)} proxies '
            f'({len(self._proxies) - before} new) from {self.url}')
        self._write_state({'attempt_ts': wall, 'lines': lines})

    def _alive(self, key, now) -> bool:
        if self._proxies[key]['dead_until'] > now:
            return False
        # benches are shared through the stats DB so a proxy marked
        # dead by one worker is skipped by all pool copies
        if self._stats_db is not None:
            until = self._stats_db.get(f'proxy:dead:{_display(key)}')
            if until and until > time.time():
                return False
        return True

    def _start_index(self, n) -> int:
        """Rotation offset: a shared counter keeps acquires evenly
        spread across ALL pool copies, not just within one task."""
        if self._stats_db is not None:
            idx = self._stats_db.increment('proxy:rr', ttl=86400)
            if idx:
                return idx % n
        return self._rr_index

    def _recently_used(self, key) -> bool:
        """Per-egress-IP pacing: True when the proxy was used less than
        per_proxy_delay ago by any worker sharing the stats DB."""
        if self._per_proxy_delay <= 0 or self._stats_db is None:
            return False
        last = self._stats_db.get(f'proxy:ts:{_display(key)}')
        return bool(last) and time.time() - last < self._per_proxy_delay

    def acquire(self) -> str:
        """Returns the next healthy proxy URL, or None if the pool is empty."""
        now = time.monotonic()
        with self._lock:
            self.refresh_list()
            if not self._proxies:
                return None
            keys = list(self._proxies.keys())
            start = self._start_index(len(keys))
            for i in range(len(keys)):
                idx = (start + i) % len(keys)
                if self._alive(keys[idx], now) and \
                        not self._recently_used(keys[idx]):
                    self._rr_index = (idx + 1) % len(keys)
                    if self._per_proxy_delay > 0 and \
                            self._stats_db is not None:
                        self._stats_db.set(
                            f'proxy:ts:{_display(keys[idx])}',
                            time.time(),
                            ttl=max(self._per_proxy_delay, 60))
                    return keys[idx]
            return None

    def report_success(self, proxy, rtt=None):
        if self._stats_db is not None:
            hp = _display(proxy)
            self._stats_db.record_proxy_result(hp, ok=True)
            # a success means the proxy is healthy - unbench it and
            # reset its consecutive-fail streak for the other workers
            # sharing pool state
            self._stats_db.delete(f'proxy:dead:{hp}')
            self._stats_db.delete(f'proxy:fails:{hp}')
            self._stats_db.record_proxy_dead(hp)
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
        hp = _display(proxy)
        shared_bench = False
        if self._stats_db is not None:
            self._stats_db.record_proxy_result(hp, ok=False)
            # the consecutive-fail streak is shared too: each task gets
            # a pickled pool copy with a fresh counter, so without this
            # a proxy failing <max_fails per task would never bench
            shared_bench = self._stats_db.increment(
                f'proxy:fails:{hp}', ttl=self.cooldown) >= self.max_fails
        now = time.monotonic()
        with self._lock:
            stats = self._proxies.get(proxy)
            if stats is None:
                return
            stats['fails'] += 1
            if stats['fails'] >= self.max_fails or shared_bench:
                stats['dead_until'] = now + self.cooldown
                stats['fails'] = 0
                if self._stats_db is not None:
                    dead_until = time.time() + self.cooldown
                    self._stats_db.set(
                        f'proxy:dead:{hp}', dead_until, ttl=self.cooldown)
                    self._stats_db.delete(f'proxy:fails:{hp}')
                    self._stats_db.record_proxy_dead(hp, dead_until)
                alive = sum(
                    1 for s in self._proxies.values()
                    if s['dead_until'] <= now)
                log.info(
                    f'Proxy {hp} benched for {self.cooldown}s '
                    f'({alive}/{len(self._proxies)} alive)')

    def stats(self):
        # _alive() also consults the shared 'proxy:dead:*' markers, so
        # this reflects benches recorded by worker pool copies, not just
        # this process's own state
        now = time.monotonic()
        alive = sum(1 for k in self._proxies if self._alive(k, now))
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
                 user_agent=None, request_delay=0.0):
        super().__init__()
        self._pool = pool
        self._status_forcelist = set(status_forcelist)
        # optional minimum seconds between requests (per process, like
        # the scraper throttle) - set for metadata lookup sessions
        self._request_delay = request_delay
        self._last_request = 0.0
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
            '_pool', '_status_forcelist',
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
                        f'Proxy {_display(proxy)} got HTTP '
                        f'{resp.status_code} for {url}')
                    self._pool.report_failure(proxy)
                    # kept open - it is returned to the caller when no
                    # direct fallback is allowed
                    last_resp = resp
                    continue
                self._pool.report_success(proxy, time.monotonic() - start)
                return resp
            except PROXY_FAILURES as e:
                log.debug(
                    f'Proxy {_display(proxy)} failed for {url}: {e}')
                self._pool.report_failure(proxy)
                last_error = e
        if self._pool.allow_direct:
            if last_error is not None or last_resp is not None:
                self._warn_direct(url, 'All proxies failed')
            elif not self._pool._proxies:
                self._warn_direct(url, 'Proxy pool is empty')
            else:
                self._warn_direct(
                    url, 'No healthy proxies (all benched or paced)')
            kwargs.pop('proxies', None)
            return super().request(method, url, **kwargs)
        if last_error is not None:
            raise last_error
        if last_resp is not None:
            return last_resp
        raise requests.exceptions.ConnectionError(
            'No healthy proxies available in the pool')

    def _warn_direct(self, url, reason):
        # the shared claim caps this at one warning per minute across
        # all workers - a fully benched pool would otherwise warn on
        # every single request
        db = self._pool._stats_db
        if db is not None and not db.claim('proxy:warn:direct', 60):
            return
        log.warning(
            f'{reason}, falling back to direct connection for {url}')


def get_proxy_session(pool: ProxyPool,
                      user_agent=None,
                      request_delay=0.0) -> RotatingProxySession:
    return RotatingProxySession(pool, user_agent=user_agent,
                                request_delay=request_delay)


def format_proxy_stats(rows, top=10) -> str:
    """Renders proxy_stats table rows (Cache.proxy_stats()) as a report:
    a summary line plus the `top` worst proxies."""
    if not rows:
        return 'no proxy usage recorded yet'
    now = time.time()
    benched = sum(1 for r in rows
                  if r['dead_until'] and r['dead_until'] > now)
    ok = sum(r['successes'] for r in rows)
    fails = sum(r['fails'] for r in rows)
    lines = [
        f'{len(rows)} proxies seen, {benched} currently benched, '
        f'{ok} proxied requests ok, {fails} failed']
    for r in rows[:top]:
        last = time.strftime('%Y-%m-%d %H:%M',
                             time.localtime(r['last_seen']))
        dead = ''
        if r['dead_until'] and r['dead_until'] > now:
            dead = ' benched-until=' + time.strftime(
                '%H:%M', time.localtime(r['dead_until']))
        lines.append(
            f"  {r['proxy']:<22} ok={r['successes']:<6} "
            f"fails={r['fails']:<5} last={last}{dead}")
    if len(rows) > top:
        lines.append(f'  ... and {len(rows) - top} more')
    return '\n'.join(lines)


def print_proxy_stats(db_path='epg_cache.sqlite'):
    """Dumps the persistent per-proxy stats table (for --proxy-stats)."""
    from py_epg.common.cache import Cache
    print(format_proxy_stats(Cache(db_path).proxy_stats()))
