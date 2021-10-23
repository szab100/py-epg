#!/usr/bin/env python3
"""Init module."""
import os
from py_epg.common import logging

log_config = os.path.dirname(os.path.abspath(__file__)) + '/logging.yaml'
logging.setup_logging(path=log_config)