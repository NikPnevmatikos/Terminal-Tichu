"""Newline-delimited JSON over TCP.

Client -> server messages carry a "cmd" key; server -> client messages an
"event" key. See README.md for the full message catalogue.
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional

DEFAULT_PORT = 4271
MAX_LINE = 64 * 1024


def encode(obj: dict) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode() + b"\n"


async def read_message(reader: asyncio.StreamReader) -> Optional[dict]:
    """One JSON message, or None on EOF/oversize/invalid framing."""
    try:
        line = await reader.readline()
    except (ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        return None
    if not line or len(line) > MAX_LINE:
        return None
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        return {}
    return msg if isinstance(msg, dict) else {}
