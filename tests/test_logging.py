#!/usr/bin/env python3
"""Tests for the logging bootstrap: yaml config, basicConfig fallback,
the custom TRACE level and get()."""

import logging

from py_epg.common.logging import get, setup_logging


class TestSetupLogging:
    def test_falls_back_to_basic_config(self, tmp_path):
        setup_logging(path=str(tmp_path / 'no-such.yaml'))
        assert logging.getLogger().level == logging.INFO

    def test_dict_config_from_yaml(self, tmp_path):
        cfg = tmp_path / 'logging.yaml'
        cfg.write_text(
            'version: 1\n'
            'root:\n'
            '  level: WARNING\n')
        setup_logging(path=str(cfg))
        assert logging.getLogger().level == logging.WARNING
        logging.getLogger().setLevel(logging.INFO)  # restore


class TestGetAndTrace:
    def test_get_returns_named_logger(self):
        assert get('x.y').name == 'x.y'

    def test_trace_level_registered(self):
        assert logging.TRACE == 5
        assert logging.getLevelName(5) == 'TRACE'

    def test_trace_callable(self):
        logger = get('trace-test')
        logger.trace('below threshold')        # filtered out
        logger.setLevel(logging.TRACE)
        logger.trace('visible')                # reaches _log body
