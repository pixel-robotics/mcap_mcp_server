"""Tests for the one-stop load_interval tool."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from mcap_mcp_server.config import ServerConfig
from mcap_mcp_server.foxglove import FoxgloveError, FoxgloveRecording
from mcap_mcp_server.server import _alias_from_path, _normalize_interval, create_server
from tests.conftest import create_simple_mcap
from tests.test_server_foxglove import FakeFoxglove, _get_tool_fn

# The fixture MCAP written by create_simple_mcap starts at ~2023-11-14T22:13:20Z
# and spans two seconds of /battery messages.
INTERVAL = ("2023-11-14T00:00:00Z", "2023-11-15T00:00:00Z")


def make_recording(
    rec_id: str = "rec_1",
    import_status: str = "complete",
    path: str = "robot/session_042.mcap",
    device: str = "pixel-bot-1",
) -> FoxgloveRecording:
    return FoxgloveRecording.from_api(
        {
            "id": rec_id,
            "key": f"key-{rec_id}",
            "path": path,
            "importStatus": import_status,
            "size": 2 * 1024 * 1024,
            "start": "2023-11-14T22:00:00Z",
            "end": "2023-11-14T23:00:00Z",
            "device": {"id": f"dev_{device}", "name": device},
        }
    )


@pytest.fixture
def fox(monkeypatch, tmp_path: Path):
    """Server wired to a FakeFoxglove; yields (load_interval, query, fake, config)."""
    fake = FakeFoxglove()
    # Real MCAP bytes so the downloaded file can actually be decoded.
    seed = tmp_path / "seed.mcap"
    create_simple_mcap(seed)
    fake.payload = seed.read_bytes()
    seed.unlink()

    monkeypatch.setattr("mcap_mcp_server.server.FoxgloveClient", lambda **kw: fake)
    config = ServerConfig(data_dir=tmp_path)
    server = create_server(config)

    load_interval_fn = _get_tool_fn(server, "load_interval")

    def load_interval(*args, **kwargs):
        return asyncio.run(load_interval_fn(*args, **kwargs))

    tools = {
        "load_interval": load_interval,
        "query": _get_tool_fn(server, "query"),
    }
    return tools, fake, config


class TestLoadInterval:
    def test_downloads_and_loads_remote_recording(self, fox):
        tools, fake, _ = fox
        fake.recordings = [make_recording()]
        result = json.loads(tools["load_interval"](*INTERVAL))
        assert result["status"] == "loaded"
        assert result["total_rows"] > 0
        assert "battery" in result["tables"]
        assert result["recordings"][0]["status"] == "imported"

    def test_loaded_data_is_queryable(self, fox):
        tools, fake, _ = fox
        fake.recordings = [make_recording()]
        tools["load_interval"](*INTERVAL)
        rows = json.loads(tools["query"]("SELECT COUNT(*) AS n FROM battery"))
        assert rows["rows"][0][0] == 100

    def test_triggers_device_upload_when_needed(self, fox):
        tools, fake, _ = fox
        fake.recordings = [make_recording(import_status="none")]
        result = json.loads(tools["load_interval"](*INTERVAL))
        assert "rec_1" in fake.import_calls
        assert result["status"] == "loaded"
        assert result["recordings"][0]["uploaded_from_device"] is True

    def test_no_upload_triggered_for_imported_recording(self, fox):
        tools, fake, _ = fox
        fake.recordings = [make_recording()]
        tools["load_interval"](*INTERVAL)
        assert fake.import_calls == []

    def test_second_call_reuses_downloaded_file(self, fox):
        tools, fake, _ = fox
        fake.recordings = [make_recording()]
        tools["load_interval"](*INTERVAL)
        result = json.loads(tools["load_interval"](*INTERVAL))
        assert len(fake.download_calls) == 1
        assert result["recordings"][0]["status"] == "already_local"

    def test_multiple_devices_require_choice(self, fox):
        tools, fake, _ = fox
        fake.recordings = [
            make_recording("rec_1", device="pixel-bot-1"),
            make_recording("rec_2", device="pixel-bot-2", path="robot/session_043.mcap"),
        ]
        result = json.loads(tools["load_interval"](*INTERVAL))
        assert "devices" in result
        assert result["devices"] == ["pixel-bot-1", "pixel-bot-2"]
        assert "error" in result

    def test_single_device_needs_no_choice(self, fox):
        tools, fake, _ = fox
        fake.recordings = [
            make_recording("rec_1"),
            make_recording("rec_2", path="robot/session_043.mcap"),
        ]
        result = json.loads(tools["load_interval"](*INTERVAL))
        assert result["status"] == "loaded"
        assert len(result["recordings"]) == 2

    def test_multiple_recordings_get_aliased_tables(self, fox):
        tools, fake, _ = fox
        fake.recordings = [
            make_recording("rec_1"),
            make_recording("rec_2", path="robot/session_043.mcap"),
        ]
        result = json.loads(tools["load_interval"](*INTERVAL))
        assert "session_042_battery" in result["tables"]
        assert "session_043_battery" in result["tables"]

    def test_upload_failure_is_reported_per_recording(self, fox):
        tools, fake, _ = fox
        fake.recordings = [make_recording(import_status="importing")]
        fake.imported_after_wait = False
        result = json.loads(tools["load_interval"](*INTERVAL))
        # The only recording failed, and no local fallback exists.
        assert "error" in result
        assert result["recordings"][0]["status"] == "error"
        assert "Timed out" in result["recordings"][0]["error"]

    def test_falls_back_to_local_files_without_foxglove(self, fox):
        tools, fake, config = fox
        fake.configured = False
        create_simple_mcap(config.data_dir / "local_run.mcap")
        result = json.loads(tools["load_interval"](*INTERVAL))
        assert result["status"] == "loaded"
        assert result["recordings"][0]["status"] == "already_local"
        assert any("Foxglove API key" in n for n in result["notes"])

    def test_falls_back_to_local_files_on_foxglove_error(self, fox):
        tools, fake, config = fox
        fake.list_error = FoxgloveError("boom")
        create_simple_mcap(config.data_dir / "local_run.mcap")
        result = json.loads(tools["load_interval"](*INTERVAL))
        assert result["status"] == "loaded"
        assert any("boom" in n for n in result["notes"])

    def test_local_file_outside_interval_is_ignored(self, fox):
        tools, fake, config = fox
        fake.configured = False
        create_simple_mcap(config.data_dir / "local_run.mcap")
        result = json.loads(
            tools["load_interval"]("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")
        )
        assert "error" in result

    def test_nothing_found_reports_error(self, fox):
        tools, _, _ = fox
        result = json.loads(tools["load_interval"](*INTERVAL))
        assert "No recordings overlap" in result["error"]

    def test_invalid_times_are_rejected(self, fox):
        tools, _, _ = fox
        assert "error" in json.loads(tools["load_interval"]("gestern", "heute"))

    def test_end_before_start_is_rejected(self, fox):
        tools, _, _ = fox
        result = json.loads(
            tools["load_interval"]("2023-11-15T00:00:00Z", "2023-11-14T00:00:00Z")
        )
        assert "end must be after start" in result["error"]

    def test_progress_is_reported(self, fox):
        tools, fake, _ = fox
        fake.recordings = [make_recording()]

        class FakeCtx:
            def __init__(self):
                self.calls = []

            async def report_progress(self, progress, total=None, message=None):
                self.calls.append((progress, total, message))

        ctx = FakeCtx()
        result = json.loads(tools["load_interval"](*INTERVAL, ctx=ctx))
        assert result["status"] == "loaded"
        messages = [m for _, _, m in ctx.calls]
        assert any("fetching recording 1/1" in m for m in messages)
        assert any("loading recording 1/1" in m for m in messages)
        assert messages[-1] == "done"

    def test_interval_is_forwarded_to_foxglove_as_utc(self, fox):
        tools, fake, _ = fox
        fake.recordings = [make_recording()]

        seen = {}
        original = fake.list_recordings

        def spy(device=None, start=None, end=None, **kw):
            seen.update({"start": start, "end": end})
            return original(device=device, start=start, end=end, **kw)

        fake.list_recordings = spy
        tools["load_interval"]("2023-11-14T01:00:00+01:00", "2023-11-15T00:00:00")
        assert seen["start"] == "2023-11-14T00:00:00Z"
        assert seen["end"] == "2023-11-15T00:00:00Z"


class TestIntervalHelpers:
    def test_normalize_interval_roundtrip(self):
        start, end = _normalize_interval("2026-08-01T10:00:00Z", "2026-08-01T11:00:00Z")
        assert start == "2026-08-01T10:00:00Z"
        assert end == "2026-08-01T11:00:00Z"

    def test_naive_times_become_utc(self):
        start, _ = _normalize_interval("2026-08-01T10:00:00", "2026-08-01T11:00:00")
        assert start == "2026-08-01T10:00:00Z"

    def test_alias_sanitizes_file_names(self):
        assert _alias_from_path(Path("a/session-042.mcap")) == "session_042"
        assert _alias_from_path(Path("2023 run.mcap")) == "r_2023_run"
        assert _alias_from_path(Path("---.mcap")) == "rec"
