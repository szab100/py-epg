#!/usr/bin/env python3
"""EPG Scraper for a specific website"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import date
from typing import Dict, List

from fake_useragent import UserAgent
from py_epg.common.requests import get_http_session
from xmltv.models import Channel, Programme

UA = UserAgent()


class EpgScraper(ABC):
    """Abstract class providing simple methods to fetch XMLTV data from an EPG website."""

    def __init__(self, name: str, proxy=None):
        super().__init__()
        self._log = logging.getLogger(name)
        self._user_agent = UA.random
        self._http = get_http_session(user_agent=self._user_agent, proxy=proxy)

    @abstractmethod
    def site_name(self) -> str:
        """Returns the site_id of the EPG website this scraper supports"""

    @abstractmethod
    def fetch_channel(self, site_id, xmltv_id, name) -> Channel:
        """Fetches and returns the requested channel object."""

    @abstractmethod
    def fetch_programs(self, channel: Channel, channel_site_id: str, fetch_date: date) -> List[Programme]:
        """
        Returns a Dict with all the programmes for the given channel and day.

        Notes:
            - All dates must be in local xmltv format with timezone info.
            - Stop times are automatically set from programs' start times.
            - The order of returned programmes is irrelevant, they will get sorted by start time.

        Parameters:
            channel_site_id: the channel's name as present on this EPG site
            date: the day of which programs need to be fetched for

        Returns:
            List[Programme]: the fetched channel and its programmes for the given 'day'.
        """
