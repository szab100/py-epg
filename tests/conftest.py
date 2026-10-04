#!/usr/bin/env python3
"""Shared fixtures/helpers for the py_epg test suite."""

import pickle
from unittest.mock import MagicMock

import pytest
from lxml import etree as ET

from py_epg.common.cache import Cache


@pytest.fixture
def cache(tmp_path):
    """A real (enabled) Cache backed by a throwaway SQLite file."""
    c = Cache(path=str(tmp_path / 'cache.sqlite'), ttls={
        'channel': 60, 'program': 60, 'meta': 60, 'listing': 60})
    yield c
    c.close()


@pytest.fixture
def disabled_cache():
    return Cache(enabled=False)


def cfg_el(xml: str):
    """Parses a config element from an XML string."""
    return ET.fromstring(xml)


def json_response(payload):
    """A MagicMock quacking like a successful requests.Response."""
    r = MagicMock()
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


def roundtrip(obj):
    """Pickle + unpickle - mirrors what multiprocessing workers see."""
    return pickle.loads(pickle.dumps(obj))
