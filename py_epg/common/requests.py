#!/usr/bin/env python3
"""Module defines request related configurations."""
from urllib3 import util

import requests
from requests import adapters


def get_http_session(
        retries=5,
        backoff_factor=20.0,
        status_forcelist=(500, 502, 504),
        session=None,
        proxy=None,
        user_agent=None,
) -> requests.Session:
    """
    Build request retry policy.
    wait bydefault to 5+ mins in 5 retries unless these settings overriden by client.
    """
    session = session or requests.Session()
    retry = util.Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
    )
    adapter = adapters.HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    if user_agent:
        session.headers.update({'User-Agent': user_agent})
    if proxy:
        session.proxies.update({'http': proxy, 'https': proxy})
    return session
