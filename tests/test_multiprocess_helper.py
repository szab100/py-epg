#!/usr/bin/env python3
"""Tests for the lxml pickling hooks used by the worker pool."""

import pickle

from lxml import etree

from py_epg.common.multiprocess_helper import (
    element_pickler, elementtree_pickler, setup_ltree_pickling)


class TestLtreePickling:
    def test_element_roundtrip(self):
        setup_ltree_pickling()
        el = etree.fromstring('<a><b x="1">t</b></a>')
        clone = pickle.loads(pickle.dumps(el))
        assert etree.tostring(clone) == etree.tostring(el)

    def test_elementtree_roundtrip(self):
        setup_ltree_pickling()
        tree = etree.ElementTree(
            etree.fromstring('<settings><x/></settings>'))
        clone = pickle.loads(pickle.dumps(tree))
        assert etree.tostring(clone.getroot()) == \
            b'<settings><x/></settings>'

    def test_picklers_return_reduction_tuples(self):
        el = etree.fromstring('<a/>')
        fn, (data,) = element_pickler(el)
        assert fn(data) is not None
        tree = etree.ElementTree(el)
        fn, (data,) = elementtree_pickler(tree)
        assert fn(data) is not None
