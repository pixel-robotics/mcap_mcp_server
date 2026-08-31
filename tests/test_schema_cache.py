"""Regression tests: decoder caches must be keyed by schema content, not schema_id.

MCAP schema ids are only unique within one file. A long-lived server loads many
files, so caching parsed schemas by id serves the wrong decoder as soon as two
files assign the same id to different schemas — every message of the second
topic then fails to decode (silently, before decode_errors reporting existed).
This is exactly what made /collision_monitor/steering_validation_debug come
back empty in production on 2026-08-31.
"""

from __future__ import annotations

import struct

import pytest

from mcap_mcp_server.decoders.base import schema_cache_key

ros2 = pytest.importorskip("mcap_ros2")

from mcap_mcp_server.decoders.ros2_decoder import Ros2Decoder  # noqa: E402

# CDR encapsulation header (little-endian) used by ROS 2.
_CDR_HEADER = b"\x00\x01\x00\x00"


def _cdr_string(value: str) -> bytes:
    raw = value.encode() + b"\x00"
    return _CDR_HEADER + struct.pack("<I", len(raw)) + raw


def _cdr_float64(value: float) -> bytes:
    return _CDR_HEADER + struct.pack("<d", value)


class TestRos2SchemaIdCollision:
    def test_same_schema_id_in_two_files_decodes_both(self):
        decoder = Ros2Decoder(flatten_depth=3)

        # File 1: schema_id 5 is std_msgs/msg/String.
        out1 = decoder.decode(
            b"string data",
            _cdr_string("hi"),
            schema_name="std_msgs/msg/String",
            schema_encoding="ros2msg",
            schema_id=5,
        )
        assert out1 == {"data": "hi"}

        # File 2: schema_id 5 is a completely different message. With an
        # id-keyed cache this reused the String decoder and failed.
        out2 = decoder.decode(
            b"float64 value",
            _cdr_float64(1.5),
            schema_name="test_msgs/msg/Val",
            schema_encoding="ros2msg",
            schema_id=5,
        )
        assert out2 == {"value": 1.5}

    def test_same_schema_under_two_ids_is_cached_once(self):
        decoder = Ros2Decoder(flatten_depth=3)
        for schema_id in (1, 42):
            out = decoder.decode(
                b"string data",
                _cdr_string("x"),
                schema_name="std_msgs/msg/String",
                schema_encoding="ros2msg",
                schema_id=schema_id,
            )
            assert out == {"data": "x"}
        assert len(decoder._decoders) == 1


class TestSchemaCacheKey:
    def test_differs_by_content(self):
        a = schema_cache_key("pkg/Msg", b"string data")
        b = schema_cache_key("pkg/Msg", b"float64 value")
        assert a != b

    def test_differs_by_name(self):
        a = schema_cache_key("pkg/A", b"string data")
        b = schema_cache_key("pkg/B", b"string data")
        assert a != b

    def test_stable(self):
        assert schema_cache_key("pkg/A", b"x") == schema_cache_key("pkg/A", b"x")
