#!/usr/bin/env python3
"""XMLTV serializer.

Local replacement for xmltv.xmltv_helpers.write_file_from_xml: identical
serializer configuration, but without that module's pkg_resources import
(deprecated/removed in modern setuptools).
"""

import pathlib

from xsdata.formats.dataclass.serializers import XmlSerializer
from xsdata.formats.dataclass.serializers.config import SerializerConfig


def write_file_from_xml(xml_file_path: pathlib.Path, serialize_clazz):
    serializer = XmlSerializer(config=SerializerConfig(
        pretty_print=True,
        encoding="UTF-8",
        xml_version="1.0",
        xml_declaration=True,
        schema_location="resources/xmltv.xsd",
        no_namespace_schema_location=None))
    with xml_file_path.open("w") as data:
        serializer.write(data, serialize_clazz)
