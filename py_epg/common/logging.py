#!/usr/bin/env python3
import logging
import logging.config
import os

import yaml


def setup_logging(
    path='logging.yaml',
    default_level=logging.INFO
):
    """Setup logging configuration

    """
    _create_trace_loglevel(logging)
    if os.path.exists(path):
        with open(path, 'rt') as f:
            config = yaml.safe_load(f.read())
        logging.config.dictConfig(config)
    else:
        logging.basicConfig(level=default_level)

def get(name):
    logger = logging.getLogger(name)
    return logger

def _create_trace_loglevel(logging):
    "Add TRACE log level and Logger.trace() method."

    logging.TRACE = 5
    logging.addLevelName(logging.TRACE, "TRACE")

    def _trace(logger, message, *args, **kwargs):
        if logger.isEnabledFor(logging.TRACE):
            logger._log(logging.TRACE, message, args, **kwargs)

    logging.Logger.trace = _trace
