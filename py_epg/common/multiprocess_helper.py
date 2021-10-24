#!/usr/bin/env python3

import copyreg as copy_reg
from io import BytesIO

from lxml import etree


def element_unpickler(data):
    return etree.fromstring(data)


def element_pickler(element):
    data = etree.tostring(element)
    return element_unpickler, (data,)


def elementtree_unpickler(data):
    data = BytesIO(data)
    return etree.parse(data)


def elementtree_pickler(tree):
    data = BytesIO()
    tree.write(data)
    return elementtree_unpickler, (data.getvalue(),)


def setup_ltree_pickling():
    copy_reg.pickle(etree._Element, element_pickler, element_unpickler)
    copy_reg.pickle(etree._ElementTree, elementtree_pickler,
                    elementtree_unpickler)
