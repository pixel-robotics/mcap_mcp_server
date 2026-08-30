"""Tests for the Foxglove Data Platform client."""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path

import pytest

from mcap_mcp_server.foxglove import (
    FoxgloveAuthError,
    FoxgloveClient,
    FoxgloveError,
    FoxgloveRecording,
    _looks_like_id,
    _mcap_stem,
)


# ---------------------------------------------------------------------------
# Fake HTTP layer
# ---------------------------------------------------------------------------


class FakeResponse(io.BytesIO):
    """Minimal stand-in for the object returned by urlopen()."""

    def __init__(self, payload: bytes, content_type: str = "application/json"):
        super().__init__(payload)
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class FakeHttp:
    """Records requests and replays canned responses keyed by 'METHOD /path'."""

    def __init__(self, routes: dict[str, object]):
        self.routes = routes
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, request, timeout=None):
        method = request.get_method()
        url = request.full_url
        path = url.split("api.foxglove.dev", 1)[-1] if "api.foxglove.dev" in url else url
        body = json.loads(request.data) if request.data else None
        self.calls.append((method, path, body))

        for key, response in self.routes.items():
            route_method, route_path = key.split(" ", 1)
            if method == route_method and path.split("?")[0] == route_path:
                if callable(response):
                    response = response(self.calls)
                if isinstance(response, Exception):
                    raise response
                if isinstance(response, FakeResponse):
                    return response
                return FakeResponse(json.dumps(response).encode())
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b"{}"))


def http_error(code: int, message: str = "boom") -> urllib.error.HTTPError:
    body = json.dumps({"error": message}).encode()
    return urllib.error.HTTPError("https://api.foxglove.dev", code, message, {}, io.BytesIO(body))


@pytest.fixture
def client(monkeypatch):
    def _make(routes: dict[str, object]) -> tuple[FoxgloveClient, FakeHttp]:
        fake = FakeHttp(routes)
        monkeypatch.setattr("urllib.request.urlopen", fake)
        return FoxgloveClient(api_key="fox_sk_test"), fake

    return _make


def recording_json(
    rec_id: str = "rec_1",
    import_status: str = "complete",
    path: str = "robot/session_042.mcap",
) -> dict:
    return {
        "id": rec_id,
        "key": "session-042",
        "path": path,
        "importStatus": import_status,
        "size": 5 * 1024 * 1024,
        "start": "2026-08-01T10:00:00Z",
        "end": "2026-08-01T10:30:00Z",
        "device": {"id": "dev_9", "name": "pixel-bot-1"},
    }


# ---------------------------------------------------------------------------
# FoxgloveRecording
# ---------------------------------------------------------------------------


class TestFoxgloveRecording:
    def test_from_api_maps_fields(self):
        rec = FoxgloveRecording.from_api(recording_json())
        assert rec.id == "rec_1"
        assert rec.key == "session-042"
        assert rec.device_name == "pixel-bot-1"
        assert rec.device_id == "dev_9"
        assert rec.size_bytes == 5 * 1024 * 1024
        assert rec.is_imported is True

    def test_from_api_tolerates_missing_fields(self):
        rec = FoxgloveRecording.from_api({"id": "rec_2"})
        assert rec.key == ""
        assert rec.size_bytes == 0
        assert rec.is_imported is False

    def test_filename_uses_path_basename(self):
        rec = FoxgloveRecording.from_api(recording_json())
        assert rec.filename == "session_042.mcap"

    def test_filename_appends_extension(self):
        rec = FoxgloveRecording.from_api({"id": "rec_3", "path": "run-7.bag"})
        assert rec.filename == "run-7.mcap"

    def test_filename_falls_back_to_key_then_id(self):
        assert FoxgloveRecording.from_api({"id": "rec_4", "key": "k9"}).filename == "k9.mcap"
        assert FoxgloveRecording.from_api({"id": "rec_5"}).filename == "rec_5.mcap"

    def test_filename_raises_without_identity(self):
        with pytest.raises(FoxgloveError):
            FoxgloveRecording(id="").filename

    def test_to_json_is_serialisable(self):
        payload = FoxgloveRecording.from_api(recording_json()).to_json()
        assert json.loads(json.dumps(payload))["size_mb"] == 5.0
        assert payload["device"] == "pixel-bot-1"


# ---------------------------------------------------------------------------
# Auth / configuration
# ---------------------------------------------------------------------------


class TestAuth:
    def test_api_key_read_from_env(self, monkeypatch):
        monkeypatch.setenv("FOXGLOVE_API_KEY", "fox_sk_env")
        assert FoxgloveClient().configured is True

    def test_alternate_env_var(self, monkeypatch):
        monkeypatch.delenv("FOXGLOVE_API_KEY", raising=False)
        monkeypatch.setenv("MCAP_FOXGLOVE_API_KEY", "fox_sk_env2")
        assert FoxgloveClient().configured is True

    def test_missing_key_raises_actionable_error(self, monkeypatch):
        monkeypatch.delenv("FOXGLOVE_API_KEY", raising=False)
        monkeypatch.delenv("MCAP_FOXGLOVE_API_KEY", raising=False)
        c = FoxgloveClient()
        assert c.configured is False
        with pytest.raises(FoxgloveAuthError, match="FOXGLOVE_API_KEY"):
            c.list_recordings()

    def test_rejected_key_raises_auth_error(self, client):
        c, _ = client({"GET /v1/recordings": http_error(403, "forbidden")})
        with pytest.raises(FoxgloveAuthError, match="rejected"):
            c.list_recordings()

    def test_bearer_header_is_sent(self, monkeypatch):
        seen = {}

        def fake_urlopen(request, timeout=None):
            seen["auth"] = request.headers.get("Authorization")
            return FakeResponse(b"[]")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        FoxgloveClient(api_key="fox_sk_abc").list_recordings()
        assert seen["auth"] == "Bearer fox_sk_abc"


# ---------------------------------------------------------------------------
# list / get / find
# ---------------------------------------------------------------------------


class TestListRecordings:
    def test_returns_recordings(self, client):
        c, _ = client({"GET /v1/recordings": [recording_json(), recording_json("rec_2")]})
        recordings = c.list_recordings()
        assert [r.id for r in recordings] == ["rec_1", "rec_2"]

    def test_accepts_wrapped_payload(self, client):
        c, _ = client({"GET /v1/recordings": {"recordings": [recording_json()]}})
        assert len(c.list_recordings()) == 1

    def test_device_name_vs_id_parameter(self, client):
        c, fake = client({"GET /v1/recordings": []})
        c.list_recordings(device="pixel-bot-1")
        c.list_recordings(device="dev_9")
        assert "deviceName=pixel-bot-1" in fake.calls[0][1]
        assert "deviceId=dev_9" in fake.calls[1][1]

    def test_limit_is_clamped(self, client):
        c, fake = client({"GET /v1/recordings": []})
        c.list_recordings(limit=99999)
        assert "limit=2000" in fake.calls[0][1]

    def test_import_status_filter(self, client):
        c, fake = client({"GET /v1/recordings": []})
        c.list_recordings(import_status="pending")
        assert "importStatus=pending" in fake.calls[0][1]

    def test_unreachable_api_raises(self, monkeypatch):
        def boom(request, timeout=None):
            raise urllib.error.URLError("no route to host")

        monkeypatch.setattr("urllib.request.urlopen", boom)
        with pytest.raises(FoxgloveError, match="Could not reach"):
            FoxgloveClient(api_key="k").list_recordings()

    def test_invalid_json_raises(self, monkeypatch):
        monkeypatch.setattr(
            "urllib.request.urlopen", lambda request, timeout=None: FakeResponse(b"not json")
        )
        with pytest.raises(FoxgloveError, match="invalid JSON"):
            FoxgloveClient(api_key="k").list_recordings()


class TestGetRecording:
    def test_returns_recording(self, client):
        c, _ = client({"GET /v1/recordings/rec_1": recording_json()})
        assert c.get_recording("rec_1").id == "rec_1"

    def test_missing_returns_none(self, client):
        c, _ = client({})
        assert c.get_recording("nope") is None

    def test_server_error_propagates(self, client):
        c, _ = client({"GET /v1/recordings/rec_1": http_error(500, "server down")})
        with pytest.raises(FoxgloveError, match="HTTP 500"):
            c.get_recording("rec_1")


class TestFindRecording:
    def test_exact_id_short_circuits(self, client):
        c, fake = client({"GET /v1/recordings/rec_1": recording_json()})
        matches = c.find_recording("rec_1")
        assert [m.id for m in matches] == ["rec_1"]
        assert all(call[1] != "/v1/recordings" for call in fake.calls)

    def test_matches_by_filename(self, client):
        c, _ = client(
            {
                "GET /v1/recordings": [
                    recording_json("rec_1", path="a/session_042.mcap"),
                    recording_json("rec_2", path="b/other.mcap"),
                ]
            }
        )
        matches = c.find_recording("session_042.mcap")
        assert [m.id for m in matches] == ["rec_1"]

    def test_matches_without_extension(self, client):
        c, _ = client({"GET /v1/recordings": [recording_json("rec_1")]})
        assert len(c.find_recording("session_042")) == 1

    def test_reports_all_ambiguous_matches(self, client):
        c, _ = client(
            {
                "GET /v1/recordings": [
                    recording_json("rec_1", path="a/session_042.mcap"),
                    recording_json("rec_2", path="b/session_042.mcap"),
                ]
            }
        )
        assert len(c.find_recording("session_042.mcap")) == 2

    def test_no_match_returns_empty(self, client):
        c, _ = client({"GET /v1/recordings": [recording_json()]})
        assert c.find_recording("missing.mcap") == []


# ---------------------------------------------------------------------------
# Import from device
# ---------------------------------------------------------------------------


class TestRequestImport:
    def test_returns_import_status(self, client):
        c, fake = client(
            {"POST /v1/recordings/rec_1/import": {"id": "rec_1", "importStatus": "pending"}}
        )
        assert c.request_import("rec_1") == "pending"
        assert fake.calls[0][0] == "POST"

    def test_defaults_to_pending_on_empty_body(self, client):
        c, _ = client({"POST /v1/recordings/rec_1/import": {}})
        assert c.request_import("rec_1") == "pending"

    def test_error_propagates(self, client):
        c, _ = client({"POST /v1/recordings/rec_1/import": http_error(409, "device offline")})
        with pytest.raises(FoxgloveError, match="device offline"):
            c.request_import("rec_1")


class TestWaitForImport:
    def test_polls_until_complete(self, client):
        statuses = iter(["importing", "importing", "complete"])
        c, _ = client(
            {
                "GET /v1/recordings/rec_1": lambda calls: recording_json(
                    import_status=next(statuses)
                )
            }
        )
        slept: list[float] = []
        rec = c.wait_for_import("rec_1", timeout_s=60, sleep=slept.append)
        assert rec.is_imported
        assert len(slept) == 2

    def test_returns_immediately_when_already_complete(self, client):
        c, _ = client({"GET /v1/recordings/rec_1": recording_json()})
        slept: list[float] = []
        c.wait_for_import("rec_1", sleep=slept.append)
        assert slept == []

    def test_failed_import_raises(self, client):
        c, _ = client({"GET /v1/recordings/rec_1": recording_json(import_status="failed")})
        with pytest.raises(FoxgloveError, match="import failure"):
            c.wait_for_import("rec_1", sleep=lambda s: None)

    def test_timeout_raises_with_retry_hint(self, client):
        c, _ = client({"GET /v1/recordings/rec_1": recording_json(import_status="importing")})
        with pytest.raises(FoxgloveError, match="Timed out"):
            c.wait_for_import("rec_1", timeout_s=0, sleep=lambda s: None)

    def test_disappeared_recording_raises(self, client):
        c, _ = client({})
        with pytest.raises(FoxgloveError, match="disappeared"):
            c.wait_for_import("rec_1", sleep=lambda s: None)


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


class TestDownload:
    def test_streams_bytes_to_destination(self, client, tmp_path: Path):
        c, fake = client(
            {"POST /v1/data/download": FakeResponse(b"MCAP-bytes", "application/octet-stream")}
        )
        dest = tmp_path / "out.mcap"
        written = c.download_recording(FoxgloveRecording.from_api(recording_json()), dest)
        assert dest.read_bytes() == b"MCAP-bytes"
        assert written == len(b"MCAP-bytes")
        assert fake.calls[0][2]["recordingId"] == "rec_1"
        assert fake.calls[0][2]["outputFormat"] == "mcap"

    def test_follows_signed_link(self, client, tmp_path: Path, monkeypatch):
        c, _ = client({"POST /v1/data/download": {"link": "https://signed.example/data.mcap"}})
        monkeypatch.setattr(
            c, "_download_link", lambda link, dest: dest.write_bytes(b"linked") or 6
        )
        dest = tmp_path / "out.mcap"
        assert c.download_recording(FoxgloveRecording.from_api(recording_json()), dest) == 6
        assert dest.read_bytes() == b"linked"

    def test_falls_back_to_stream_endpoint(self, client, tmp_path: Path):
        c, fake = client(
            {"POST /v1/data/stream": FakeResponse(b"legacy", "application/octet-stream")}
        )
        dest = tmp_path / "out.mcap"
        c.download_recording(FoxgloveRecording.from_api(recording_json()), dest)
        assert [call[1] for call in fake.calls] == ["/v1/data/download", "/v1/data/stream"]
        assert dest.read_bytes() == b"legacy"

    def test_topics_and_time_window_forwarded(self, client, tmp_path: Path):
        c, fake = client(
            {"POST /v1/data/download": FakeResponse(b"x", "application/octet-stream")}
        )
        c.download_recording(
            FoxgloveRecording.from_api(recording_json()),
            tmp_path / "out.mcap",
            topics=["/battery"],
            start="2026-08-01T10:00:00Z",
            end="2026-08-01T10:05:00Z",
        )
        body = fake.calls[0][2]
        assert body["topics"] == ["/battery"]
        assert body["start"] == "2026-08-01T10:00:00Z"
        assert body["end"] == "2026-08-01T10:05:00Z"

    def test_missing_link_raises(self, client, tmp_path: Path):
        c, _ = client({"POST /v1/data/download": {"status": "ok"}})
        with pytest.raises(FoxgloveError, match="no download link"):
            c.download_recording(
                FoxgloveRecording.from_api(recording_json()), tmp_path / "out.mcap"
            )

    def test_empty_download_raises_and_cleans_up(self, client, tmp_path: Path):
        c, _ = client({"POST /v1/data/download": FakeResponse(b"", "application/octet-stream")})
        dest = tmp_path / "out.mcap"
        with pytest.raises(FoxgloveError, match="empty file"):
            c.download_recording(FoxgloveRecording.from_api(recording_json()), dest)
        assert not dest.exists()
        assert not dest.with_name(dest.name + ".part").exists()

    def test_partial_file_is_not_left_behind_on_success(self, client, tmp_path: Path):
        c, _ = client(
            {"POST /v1/data/download": FakeResponse(b"data", "application/octet-stream")}
        )
        dest = tmp_path / "nested" / "out.mcap"
        c.download_recording(FoxgloveRecording.from_api(recording_json()), dest)
        assert dest.is_file()
        assert list(dest.parent.iterdir()) == [dest]

    def test_server_error_is_not_retried_as_fallback(self, client, tmp_path: Path):
        c, fake = client({"POST /v1/data/download": http_error(500, "boom")})
        with pytest.raises(FoxgloveError, match="HTTP 500"):
            c.download_recording(
                FoxgloveRecording.from_api(recording_json()), tmp_path / "out.mcap"
            )
        assert len(fake.calls) == 1

    def test_download_link_http_error(self, client, tmp_path: Path, monkeypatch):
        c, _ = client({})
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda request, timeout=None: (_ for _ in ()).throw(http_error(403, "expired")),
        )
        with pytest.raises(FoxgloveError, match="rejected"):
            c._download_link("https://signed.example/x", tmp_path / "x.mcap")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    @pytest.mark.parametrize("value", ["dev_abc", "rec_123"])
    def test_looks_like_id(self, value: str):
        assert _looks_like_id(value) is True

    @pytest.mark.parametrize("value", ["pixel-bot-1", "my robot_2", "1_2"])
    def test_does_not_look_like_id(self, value: str):
        assert _looks_like_id(value) is False

    def test_mcap_stem(self):
        assert _mcap_stem("a/b/session_1.mcap") == "session_1"
        assert _mcap_stem("session_1") == "session_1"
        assert _mcap_stem("") == ""
