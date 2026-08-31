"""Tests for the Foxglove MCP tools: list_foxglove_recordings, import_foxglove_recording."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcap_mcp_server.config import ServerConfig
from mcap_mcp_server.foxglove import FoxgloveError, FoxgloveRecording
from mcap_mcp_server.server import (
    _find_local_recording,
    _import_filename,
    _resolve_foxglove_recording,
    create_server,
)
from tests.conftest import create_simple_mcap


def _get_tool_fn(server, name: str):
    """Extract a tool's callable from the FastMCP server by name."""
    import asyncio

    return asyncio.run(server.get_tool(name)).fn


def make_recording(
    rec_id: str = "rec_1",
    import_status: str = "complete",
    path: str = "robot/session_042.mcap",
) -> FoxgloveRecording:
    return FoxgloveRecording.from_api(
        {
            "id": rec_id,
            "key": f"key-{rec_id}",
            "path": path,
            "importStatus": import_status,
            "size": 2 * 1024 * 1024,
            "start": "2026-08-01T10:00:00Z",
            "end": "2026-08-01T10:30:00Z",
            "device": {"id": "dev_9", "name": "pixel-bot-1"},
        }
    )


class FakeFoxglove:
    """Stands in for FoxgloveClient inside the server closure."""

    def __init__(self, **kwargs):
        self.configured = True
        self.recordings: list[FoxgloveRecording] = []
        self.list_error: Exception | None = None
        self.download_error: Exception | None = None
        self.import_calls: list[str] = []
        self.download_calls: list[dict] = []
        self.imported_after_wait = True
        self.payload = b"fake-mcap-bytes"

    # -- FoxgloveClient interface --

    def list_recordings(self, device=None, start=None, end=None, import_status=None, limit=50):
        if self.list_error:
            raise self.list_error
        return list(self.recordings)

    def get_recording(self, key_or_id):
        for rec in self.recordings:
            if key_or_id in (rec.id, rec.key):
                return rec
        return None

    def find_recording(self, recording, device=None, start=None, end=None, limit=200):
        exact = self.get_recording(recording)
        if exact is not None:
            return [exact]
        wanted = Path(recording).name
        return [r for r in self.recordings if Path(r.path).name == wanted]

    def request_import(self, key_or_id):
        self.import_calls.append(key_or_id)
        return "importing"

    def wait_for_import(self, key_or_id, timeout_s=900, poll_interval_s=5.0, sleep=None):
        rec = self.get_recording(key_or_id)
        if not self.imported_after_wait:
            raise FoxgloveError("Timed out after 900s waiting for recording to upload")
        rec.import_status = "complete"
        return rec

    def download_recording(self, recording, dest, topics=None, start=None, end=None):
        if self.download_error:
            raise self.download_error
        self.download_calls.append(
            {"id": recording.id, "dest": dest, "topics": topics, "start": start, "end": end}
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.payload)
        return len(self.payload)


@pytest.fixture
def fox(monkeypatch, tmp_path: Path):
    """Server wired to a FakeFoxglove; yields (tools, fake, config)."""
    fake = FakeFoxglove()
    monkeypatch.setattr("mcap_mcp_server.server.FoxgloveClient", lambda **kw: fake)
    config = ServerConfig(data_dir=tmp_path)
    server = create_server(config)
    tools = {
        "list": _get_tool_fn(server, "list_foxglove_recordings"),
        "import": _get_tool_fn(server, "import_foxglove_recording"),
        "list_local": _get_tool_fn(server, "list_recordings"),
    }
    return tools, fake, config


# ---------------------------------------------------------------------------
# list_foxglove_recordings
# ---------------------------------------------------------------------------


class TestListFoxgloveRecordings:
    def test_lists_remote_recordings(self, fox):
        tools, fake, _ = fox
        fake.recordings = [make_recording(), make_recording("rec_2", "importing")]
        result = json.loads(tools["list"]())
        assert result["count"] == 2
        assert result["recordings"][0]["id"] == "rec_1"

    def test_flags_recordings_needing_device_upload(self, fox):
        tools, fake, _ = fox
        fake.recordings = [
            make_recording("rec_1", "complete"),
            make_recording("rec_2", "none", path="robot/session_043.mcap"),
        ]
        entries = json.loads(tools["list"]())["recordings"]
        assert entries[0]["needs_device_upload"] is False
        assert entries[1]["needs_device_upload"] is True

    def test_reports_existing_local_copy(self, fox):
        tools, fake, config = fox
        fake.recordings = [make_recording()]
        create_simple_mcap(config.data_dir / "session_042.mcap")
        entry = json.loads(tools["list"]())["recordings"][0]
        assert entry["local_path"].endswith("session_042.mcap")

    def test_local_path_is_none_when_absent(self, fox):
        tools, fake, _ = fox
        fake.recordings = [make_recording()]
        assert json.loads(tools["list"]())["recordings"][0]["local_path"] is None

    def test_api_error_is_reported_not_raised(self, fox):
        tools, fake, _ = fox
        fake.list_error = FoxgloveError("No Foxglove API key configured")
        result = json.loads(tools["list"]())
        assert "No Foxglove API key" in result["error"]


# ---------------------------------------------------------------------------
# import_foxglove_recording
# ---------------------------------------------------------------------------


class TestImportFoxgloveRecording:
    def test_requires_an_identifier(self, fox):
        tools, _, _ = fox
        result = json.loads(tools["import"]())
        assert "error" in result

    def test_existing_local_file_short_circuits(self, fox):
        tools, fake, config = fox
        create_simple_mcap(config.data_dir / "session_042.mcap")
        result = json.loads(tools["import"](recording="session_042.mcap"))
        assert result["status"] == "already_local"
        assert fake.download_calls == []

    def test_downloads_imported_recording(self, fox):
        tools, fake, config = fox
        fake.recordings = [make_recording()]
        result = json.loads(tools["import"](recording="rec_1"))
        assert result["status"] == "imported"
        assert result["uploaded_from_device"] is False
        assert Path(result["path"]).read_bytes() == fake.payload
        assert Path(result["path"]).parent == config.foxglove_dir

    def test_triggers_device_upload_when_not_imported(self, fox):
        tools, fake, _ = fox
        fake.recordings = [make_recording(import_status="none")]
        result = json.loads(tools["import"](recording="rec_1"))
        assert fake.import_calls == ["rec_1"]
        assert result["uploaded_from_device"] is True
        assert result["status"] == "imported"

    def test_no_import_requested_for_complete_recording(self, fox):
        tools, fake, _ = fox
        fake.recordings = [make_recording(import_status="complete")]
        tools["import"](recording="rec_1")
        assert fake.import_calls == []

    def test_wait_false_returns_after_triggering_upload(self, fox):
        tools, fake, _ = fox
        fake.recordings = [make_recording(import_status="pending")]
        result = json.loads(tools["import"](recording="rec_1", wait=False))
        assert result["status"] == "import_started"
        assert fake.import_calls == ["rec_1"]
        assert fake.download_calls == []

    def test_upload_timeout_is_reported(self, fox):
        tools, fake, _ = fox
        fake.recordings = [make_recording(import_status="importing")]
        fake.imported_after_wait = False
        result = json.loads(tools["import"](recording="rec_1"))
        assert "Timed out" in result["error"]

    def test_resolves_by_file_name(self, fox):
        tools, fake, _ = fox
        fake.recordings = [make_recording()]
        result = json.loads(tools["import"](recording="session_042.mcap"))
        assert result["recording"]["id"] == "rec_1"

    def test_unknown_recording_reports_error(self, fox):
        tools, _, _ = fox
        result = json.loads(tools["import"](recording="nope.mcap"))
        assert "No Foxglove recording found" in result["error"]

    def test_ambiguous_device_window_lists_candidates(self, fox):
        tools, fake, _ = fox
        fake.recordings = [make_recording("rec_1"), make_recording("rec_2")]
        result = json.loads(tools["import"](device="pixel-bot-1"))
        assert "2 Foxglove recordings match" in result["error"]
        assert len(result["candidates"]) == 2

    def test_device_window_with_single_match_downloads(self, fox):
        tools, fake, _ = fox
        fake.recordings = [make_recording()]
        result = json.loads(
            tools["import"](
                device="pixel-bot-1", start="2026-08-01T00:00:00Z", end="2026-08-02T00:00:00Z"
            )
        )
        assert result["status"] == "imported"

    def test_topics_and_window_forwarded_to_download(self, fox):
        tools, fake, _ = fox
        fake.recordings = [make_recording()]
        tools["import"](
            recording="rec_1",
            topics=["/battery"],
            start="2026-08-01T10:00:00Z",
            end="2026-08-01T10:05:00Z",
        )
        call = fake.download_calls[0]
        assert call["topics"] == ["/battery"]
        assert call["start"] == "2026-08-01T10:00:00Z"

    def test_second_call_reuses_downloaded_file(self, fox):
        tools, fake, _ = fox
        fake.recordings = [make_recording()]
        tools["import"](recording="rec_1")
        result = json.loads(tools["import"](recording="rec_1"))
        assert result["status"] == "already_local"
        assert len(fake.download_calls) == 1

    def test_force_redownloads(self, fox):
        tools, fake, _ = fox
        fake.recordings = [make_recording()]
        tools["import"](recording="rec_1")
        result = json.loads(tools["import"](recording="rec_1", force=True))
        assert result["status"] == "imported"
        assert len(fake.download_calls) == 2

    def test_filtered_import_gets_its_own_file_name(self, fox):
        tools, fake, _ = fox
        fake.recordings = [make_recording()]
        result = json.loads(tools["import"](recording="rec_1", topics=["/battery"]))
        assert result["file"].startswith("session_042_part-")
        assert result["file"].endswith(".mcap")

    def test_filtered_file_does_not_satisfy_full_import(self, fox):
        tools, fake, _ = fox
        fake.recordings = [make_recording()]
        tools["import"](recording="rec_1", topics=["/battery"])
        result = json.loads(tools["import"](recording="rec_1"))
        assert result["status"] == "imported"
        assert result["file"] == "session_042.mcap"
        assert len(fake.download_calls) == 2

    def test_repeating_the_same_filter_reuses_the_file(self, fox):
        tools, fake, _ = fox
        fake.recordings = [make_recording()]
        tools["import"](recording="rec_1", topics=["/battery"])
        result = json.loads(tools["import"](recording="rec_1", topics=["/battery"]))
        assert result["status"] == "already_local"
        assert len(fake.download_calls) == 1

    def test_download_error_is_reported(self, fox):
        tools, fake, _ = fox
        fake.recordings = [make_recording()]
        fake.download_error = FoxgloveError("Foxglove returned an empty file")
        result = json.loads(tools["import"](recording="rec_1"))
        assert "empty file" in result["error"]

    def test_imported_file_is_visible_to_list_recordings(self, fox):
        tools, fake, config = fox
        fake.recordings = [make_recording()]
        # A real MCAP body so the local index can summarise it.
        dest = config.foxglove_dir / "session_042.mcap"
        dest.parent.mkdir(parents=True, exist_ok=True)
        create_simple_mcap(dest)
        fake.payload = dest.read_bytes()
        dest.unlink()

        tools["import"](recording="rec_1")
        listed = json.loads(tools["list_local"]())
        assert [entry["file"] for entry in listed] == ["session_042.mcap"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestFindLocalRecording:
    def test_finds_file_in_data_dir(self, tmp_path: Path):
        create_simple_mcap(tmp_path / "run.mcap")
        config = ServerConfig(data_dir=tmp_path)
        assert _find_local_recording("run.mcap", config) == tmp_path / "run.mcap"

    def test_finds_file_in_foxglove_dir(self, tmp_path: Path):
        config = ServerConfig(data_dir=tmp_path)
        config.foxglove_dir.mkdir(parents=True)
        create_simple_mcap(config.foxglove_dir / "run.mcap")
        assert _find_local_recording("run.mcap", config) == config.foxglove_dir / "run.mcap"

    def test_finds_file_in_subdirectory(self, tmp_path: Path):
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        create_simple_mcap(nested / "run.mcap")
        config = ServerConfig(data_dir=tmp_path)
        assert _find_local_recording("run.mcap", config) == nested / "run.mcap"

    def test_appends_mcap_extension(self, tmp_path: Path):
        create_simple_mcap(tmp_path / "run.mcap")
        config = ServerConfig(data_dir=tmp_path)
        assert _find_local_recording("run", config) == tmp_path / "run.mcap"

    def test_absolute_path_is_used_directly(self, tmp_path: Path):
        target = tmp_path / "elsewhere.mcap"
        create_simple_mcap(target)
        config = ServerConfig(data_dir=tmp_path / "empty")
        assert _find_local_recording(str(target), config) == target

    def test_returns_none_when_absent(self, tmp_path: Path):
        assert _find_local_recording("missing.mcap", ServerConfig(data_dir=tmp_path)) is None

    def test_empty_name_returns_none(self, tmp_path: Path):
        assert _find_local_recording("", ServerConfig(data_dir=tmp_path)) is None


class TestResolveFoxgloveRecording:
    def test_single_match_returns_recording(self):
        fake = FakeFoxglove()
        fake.recordings = [make_recording()]
        result = _resolve_foxglove_recording(fake, "rec_1", None, None, None)
        assert isinstance(result, FoxgloveRecording)

    def test_no_match_returns_error_dict(self):
        result = _resolve_foxglove_recording(FakeFoxglove(), "rec_1", None, None, None)
        assert "error" in result and "hint" in result

    def test_ambiguity_caps_candidate_list(self):
        fake = FakeFoxglove()
        fake.recordings = [make_recording(f"rec_{i}") for i in range(30)]
        result = _resolve_foxglove_recording(fake, None, "pixel-bot-1", None, None)
        assert len(result["candidates"]) == 20


class TestImportFilename:
    def test_unfiltered_uses_recording_name(self):
        assert _import_filename(make_recording(), None, None, None) == "session_042.mcap"

    def test_filter_adds_stable_suffix(self):
        first = _import_filename(make_recording(), ["/battery"], None, None)
        second = _import_filename(make_recording(), ["/battery"], None, None)
        assert first == second
        assert first != "session_042.mcap"
        assert first.endswith(".mcap")

    def test_topic_order_does_not_matter(self):
        a = _import_filename(make_recording(), ["/imu", "/battery"], None, None)
        b = _import_filename(make_recording(), ["/battery", "/imu"], None, None)
        assert a == b

    def test_different_filters_differ(self):
        a = _import_filename(make_recording(), ["/battery"], None, None)
        b = _import_filename(make_recording(), ["/imu"], None, None)
        c = _import_filename(make_recording(), None, "2026-08-01T10:00:00Z", None)
        assert len({a, b, c}) == 3
