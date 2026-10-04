#!/usr/bin/env python3
"""Module defines request related configurations."""
from urllib3 import util

import requests
from requests import adapters


def get_http_session(
        retries=5,
        backoff_factor=20.0,
        status_forcelist=(403, 429, 500, 502, 503, 504),
        session=None,
        proxy=None,
        user_agent=None,
        retry_after_max=180,
) -> requests.Session:
    """
    Build request retry policy.
    wait bydefault to 5+ mins in 5 retries unless these settings overriden by client.
    retry_after_max caps server-sent Retry-After waits (urllib3's default
    is 6 hours, which would stall a worker for hours on a rate-limit ban).
    """
    session = session or requests.Session()
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
