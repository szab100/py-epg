#!/usr/bin/env python3
"""EPG Scraper for a specific website"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from datetime import date
from typing import Dict, List

from fake_useragent import UserAgent
from py_epg.common.cache import NULL_CACHE
from py_epg.common.proxy import ProxyPool, get_proxy_session
from py_epg.common.requests import get_http_session
from xmltv.models import Channel, Programme

UA = UserAgent()


class EpgScraper(ABC):
    """Abstract class providing simple methods to fetch XMLTV data from an EPG website."""

    def __init__(self, name: str, proxy=None, user_agent=None, cache=None,
                 metadata=None, request_delay=0.0):
        super().__init__()
        self._log = logging.getLogger(name)
        self._user_agent = user_agent if user_agent else UA.random
        self._cache = cache if cache is not None else NULL_CACHE
        self._metadata = metadata
        # Minimum seconds between requests *per worker process* - each
        # pool worker throttles independently, so aggregate rate is
        # roughly pool-size / request_delay.
        self._request_delay = request_delay or 0.0
        self._last_request = 0.0
        if isinstance(proxy, ProxyPool):
            self._http = get_proxy_session(
                pool=proxy, user_agent=self._user_agent)
            self._timeout = proxy.timeout
        else:
            self._http = get_http_session(
                user_agent=self._user_agent, proxy=proxy)
            self._timeout = 60

    def _throttle(self):
        """Sleeps until request_delay has elapsed since the last request."""
        if self._request_delay <= 0:
            return
        wait = self._request_delay - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    @abstractmethod
    def site_name(self) -> str:
        """Returns the site_id of the EPG website this scraper supports"""

    def today(self) -> date:
        """
        Returns 'today' in the timezone relevant to this EPG site.
        Override in scrapers targeting sites in a specific timezone so that
        results are consistent regardless of the machine's local timezone.
        """
        return date.today()

    @abstractmethod
    def fetch_channel(self, site_id, xmltv_id, name) -> Channel:
        """Fetches and returns the requested channel object."""

    @abstractmethod
    def fetch_programs(self, channel: Channel, channel_site_id: str, fetch_date: date) -> List[Programme]:
        """
        Returns a list of all programs for the given channel and day.

        Notes:
            - All dates must be in local xmltv format with timezone info.
            - Stop times are automatically set from programs' start times.
            - The order of returned programs is irrelevant, they are sorted by channel & start time.

        Parameters:
            channel_site_id: the channel's name as present on this EPG site
            date: the day of which programs need to be fetched for

        Returns:
            List[Programme]: the fetched channel and its programmes for the given 'day'.
        """
