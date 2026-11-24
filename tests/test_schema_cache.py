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

from mcap_mcp_server.decoders.ros2_decoder import Ros2Decoder

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


rosbags = pytest.importorskip("rosbags")

from mcap_mcp_server.decoders.ros2_rosbags_decoder import Ros2RosbagsDecoder


class TestRos2RosbagsSchemaIdCollision:
    """Same collision for the rosbags-backed decoder used by default (AMRNAV-8483)."""

    def test_same_schema_id_in_two_files_decodes_both(self):
        decoder = Ros2RosbagsDecoder(flatten_depth=3)
        out1 = decoder.decode(
            b"string data",
            _cdr_string("hi"),
            schema_name="std_msgs/msg/String",
            schema_encoding="ros2msg",
            schema_id=5,
        )
        assert out1["data"] == "hi"

        out2 = decoder.decode(
            b"float64 value",
            _cdr_float64(1.5),
            schema_name="test_msgs/msg/Val",
            schema_encoding="ros2msg",
            schema_id=5,
        )
        assert out2["value"] == 1.5

    def test_same_type_name_with_changed_definition(self):
        # A message definition changed between two releases: the second file
        # carries the same type name with a different layout. rosbags refuses
        # to re-register it, so the content-keyed fallback must decode it.
        decoder = Ros2RosbagsDecoder(flatten_depth=3)
        out1 = decoder.decode(
            b"float64 value",
            _cdr_float64(2.5),
            schema_name="test_msgs/msg/Changed",
            schema_encoding="ros2msg",
            schema_id=3,
        )
        assert out1["value"] == 2.5

        out2 = decoder.decode(
            b"string value",
            _cdr_string("v2"),
            schema_name="test_msgs/msg/Changed",
            schema_encoding="ros2msg",
            schema_id=3,
        )
        assert out2["value"] == "v2"


# ---------------------------------------------------------------------------
# End to end: two files loaded into one long-lived server
# ---------------------------------------------------------------------------


def _write_ros2_mcap(path, schema_name: str, schema_text: str, topic: str, payloads):
    from mcap.writer import Writer

    base_time_ns = 1_700_000_000_000_000_000
    with open(path, "wb") as f:
        writer = Writer(f)
        writer.start()
        sid = writer.register_schema(
            name=schema_name, encoding="ros2msg", data=schema_text.encode()
        )
        cid = writer.register_channel(topic=topic, message_encoding="cdr", schema_id=sid)
        for i, data in enumerate(payloads):
            ts = base_time_ns + i * 20_000_000
            writer.add_message(channel_id=cid, log_time=ts, data=data, publish_time=ts)
        writer.finish()
    return sid


class TestLoadTwoFilesSameSchemaId:
    def test_second_file_decodes_with_its_own_schema(self, tmp_path):
        import json

        from mcap_mcp_server.config import ServerConfig
        from mcap_mcp_server.server import create_server

        sid_a = _write_ros2_mcap(
            tmp_path / "a.mcap",
            "std_msgs/msg/String",
            "string data",
            "/rosout_like",
            [_cdr_string(f"m{i}") for i in range(5)],
        )
        sid_b = _write_ros2_mcap(
            tmp_path / "b.mcap",
            "test_msgs/msg/Val",
            "float64 value",
            "/value",
            [_cdr_float64(float(i)) for i in range(7)],
        )
        assert sid_a == sid_b  # the collision this test is about

        server = create_server(ServerConfig(data_dir=tmp_path))
        tools = {t.name: t.fn for t in server._tool_manager._tools.values()}
        load, query = tools["load_recording"], tools["query"]

        res_a = json.loads(load(file="a.mcap", alias="a"))
        assert res_a["tables"]["a_rosout_like"]["rows"] == 5
        assert "decode_errors" not in res_a

        res_b = json.loads(load(file="b.mcap", alias="b"))
        assert "decode_errors" not in res_b
        assert res_b["tables"]["b_value"]["rows"] == 7

        rows = json.loads(query(sql="SELECT sum(value) AS s FROM b_value"))
        assert rows["columns"] == ["s"]
        assert rows["rows"] == [[21.0]]
