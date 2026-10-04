#!/usr/bin/env python3

import argparse

import pytest
from bs4 import BeautifulSoup

from py_epg.common.utils import argparse_str2bool, clean_text


class TestStr2Bool:
    @pytest.mark.parametrize('v', ['yes', 'true', 't', 'y', '1', 'True',
                                   'YES'])
    def test_true_values(self, v):
        assert argparse_str2bool(v) is True

    @pytest.mark.parametrize('v', ['no', 'false', 'f', 'n', '0', 'False'])
    def test_false_values(self, v):
        assert argparse_str2bool(v) is False

    def test_bool_passthrough(self):
        assert argparse_str2bool(True) is True
        assert argparse_str2bool(False) is False

    def test_invalid_raises(self):
        with pytest.raises(argparse.ArgumentTypeError):
            argparse_str2bool('maybe')


class TestCleanText:
    def test_br_becomes_newline(self):
        soup = BeautifulSoup('<div>line1<br>line2</div>', 'html.parser')
        assert clean_text(soup.div) == 'line1\nline2'

    def test_nested_markup_flattened(self):
        soup = BeautifulSoup('<div>a <b>bold</b> tail</div>',
                             'html.parser')
        assert clean_text(soup.div) == 'aboldtail'
