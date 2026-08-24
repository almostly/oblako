"""Encoder for the AWS ``vnd.amazon.eventstream`` binary framing.

Bedrock's streaming operations (InvokeModelWithResponseStream, ConverseStream)
return a stream of framed messages that boto3's ``EventStream`` parser decodes.
Each frame is: an 8-byte prelude (total length, headers length), a 4-byte prelude
CRC32, the headers, the payload, and a 4-byte message CRC32. We only ever emit
string headers (``:message-type``, ``:event-type``, ``:content-type``).
"""

from __future__ import annotations

import json
import struct
import zlib

_HEADER_TYPE_STRING = 7


def _string_header(name: str, value: str) -> bytes:
    name_bytes = name.encode("utf-8")
    value_bytes = value.encode("utf-8")
    return (
        bytes([len(name_bytes)])
        + name_bytes
        + bytes([_HEADER_TYPE_STRING])
        + struct.pack(">H", len(value_bytes))
        + value_bytes
    )


def encode_event(event_type: str, payload: dict | bytes) -> bytes:
    """Frame one event (``:event-type: <event_type>``) as an eventstream message."""
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    headers = (
        _string_header(":message-type", "event")
        + _string_header(":event-type", event_type)
        + _string_header(":content-type", "application/json")
    )
    # total = prelude(8) + prelude_crc(4) + headers + payload + message_crc(4)
    total_length = 16 + len(headers) + len(body)
    prelude = struct.pack(">II", total_length, len(headers))
    prelude_crc = struct.pack(">I", zlib.crc32(prelude) & 0xFFFFFFFF)
    without_crc = prelude + prelude_crc + headers + body
    message_crc = struct.pack(">I", zlib.crc32(without_crc) & 0xFFFFFFFF)
    return without_crc + message_crc
