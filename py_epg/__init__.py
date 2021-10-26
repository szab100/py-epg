#!/usr/bin/env python3
"""Init module."""
import logging
import os
from pprint import pprint

from py_epg.common.logging import setup_logging

log_config = os.path.dirname(os.path.abspath(__file__)) + '/logging.yaml'
setup_logging(path=log_config)
