#!/usr/bin/env python3
from dataclasses import dataclass, field
from typing import List

from xmltv.models import Channel


@dataclass(frozen=True)
class ChannelKey:
    id: str = field(compare=True, hash=True)
    channel: Channel = field(compare=False, hash=False)
