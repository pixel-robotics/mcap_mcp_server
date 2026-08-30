"""Foxglove Data Platform client: discover, import, and download recordings.

Recordings produced by a robot are often still sitting on the device (or on an
edge site) rather than in the Foxglove primary site. Such a recording has an
``importStatus`` other than ``complete`` and cannot be downloaded yet. This
module wraps the three REST calls needed to get a local ``.mcap`` file anyway:

1. ``GET  /v1/recordings``            -- find the recording
2. ``POST /v1/recordings/{id}/import`` -- ask the device/edge site to upload it
3. ``POST /v1/data/download``          -- fetch the imported MCAP bytes

Only the standard library is used, so the server keeps its dependency set.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "https://api.foxglove.dev"

#: Recording is downloadable only once the import has reached this status.
IMPORT_COMPLETE = "complete"
IMPORT_FAILED = "failed"

#: Endpoints tried, in order, to fetch recording bytes. Older deployments of the
#: Foxglove API only expose ``/v1/data/stream``.
_DOWNLOAD_PATHS = ("/v1/data/download", "/v1/data/stream")

_API_KEY_ENV_VARS = ("FOXGLOVE_API_KEY", "MCAP_FOXGLOVE_API_KEY")


class FoxgloveError(RuntimeError):
    """Raised when the Foxglove API cannot fulfil a request."""


class FoxgloveAuthError(FoxgloveError):
    """Raised when no API key is configured or the key is rejected."""


@dataclass
class FoxgloveRecording:
    """A recording as described by the Foxglove Data Platform."""

    id: str
    key: str = ""
    path: str = ""
    import_status: str = ""
    size_bytes: int = 0
    start: str = ""
    end: str = ""
    device_id: str = ""
    device_name: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "FoxgloveRecording":
        device = data.get("device") or {}
        return cls(
            id=str(data.get("id", "")),
            key=str(data.get("key") or ""),
            path=str(data.get("path") or ""),
            import_status=str(data.get("importStatus") or ""),
            size_bytes=int(data.get("size") or 0),
            start=str(data.get("start") or ""),
            end=str(data.get("end") or ""),
            device_id=str(device.get("id") or data.get("deviceId") or ""),
            device_name=str(device.get("name") or ""),
            raw=data,
        )

    @property
    def is_imported(self) -> bool:
        return self.import_status == IMPORT_COMPLETE

    @property
    def filename(self) -> str:
        """Best-effort local filename for this recording, always ``.mcap``."""
        stem = Path(self.path).name or self.key or self.id
        if not stem:
            raise FoxgloveError("Recording has neither path, key, nor id")
        if not stem.endswith(".mcap"):
            stem = f"{Path(stem).stem or stem}.mcap"
        return stem

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "key": self.key,
            "path": self.path,
            "filename": self.filename if (self.path or self.key or self.id) else "",
            "import_status": self.import_status,
            "size_mb": round(self.size_bytes / (1024 * 1024), 1),
            "start": self.start,
            "end": self.end,
            "device": self.device_name or self.device_id,
            "device_id": self.device_id,
        }


class FoxgloveClient:
    """Minimal REST client for the Foxglove Data Platform."""

    def __init__(
        self,
        api_key: str | None = None,
        api_url: str = DEFAULT_API_URL,
        timeout_s: int = 60,
    ) -> None:
        self._api_key = api_key or _api_key_from_env()
        self._api_url = api_url.rstrip("/")
        self._timeout_s = timeout_s

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    # -- low level ---------------------------------------------------------

    def _require_key(self) -> str:
        if not self._api_key:
            raise FoxgloveAuthError(
                "No Foxglove API key configured. Set the FOXGLOVE_API_KEY environment "
                "variable (or [foxglove] api_key in mcap-mcp-server.toml) to a key "
                "created at https://app.foxglove.dev/~/settings/apikeys."
            )
        return self._api_key

    def _open(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        accept: str = "application/json",
    ):
        """Issue a request and return the open response object."""
        url = f"{self._api_url}{path}"
        if params:
            query = {k: v for k, v in params.items() if v is not None}
            if query:
                url = f"{url}?{urllib.parse.urlencode(query, doseq=True)}"

        data = None
        headers = {
            "Authorization": f"Bearer {self._require_key()}",
            "Accept": accept,
        }
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        logger.debug("Foxglove %s %s", method, url)
        try:
            return urllib.request.urlopen(request, timeout=self._timeout_s)
        except urllib.error.HTTPError as exc:
            raise _http_error(exc, method, url) from exc
        except urllib.error.URLError as exc:
            raise FoxgloveError(
                f"Could not reach the Foxglove API at {self._api_url}: {exc.reason}"
            ) from exc

    def _request_json(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        with self._open(method, path, params=params, body=body) as response:
            payload = response.read()
        if not payload:
            return {}
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise FoxgloveError(f"Foxglove API returned invalid JSON for {path}") from exc

    # -- recordings --------------------------------------------------------

    def list_recordings(
        self,
        device: str | None = None,
        start: str | None = None,
        end: str | None = None,
        import_status: str | None = None,
        limit: int = 50,
        path_prefix: str | None = None,
    ) -> list[FoxgloveRecording]:
        """List recordings, newest first."""
        params: dict[str, Any] = {
            "limit": max(1, min(limit, 2000)),
            "sortBy": "start",
            "sortOrder": "desc",
            "start": start,
            "end": end,
            "importStatus": import_status,
        }
        if device:
            # A caller may hand us either a device id ("dev_...") or a name.
            params["deviceId" if _looks_like_id(device) else "deviceName"] = device
        if path_prefix:
            params["path"] = path_prefix

        data = self._request_json("GET", "/v1/recordings", params=params)
        records = data if isinstance(data, list) else data.get("recordings", [])
        return [FoxgloveRecording.from_api(item) for item in records]

    def get_recording(self, key_or_id: str) -> FoxgloveRecording | None:
        """Fetch a single recording by id or key, or None if it does not exist."""
        quoted = urllib.parse.quote(key_or_id, safe="")
        try:
            data = self._request_json("GET", f"/v1/recordings/{quoted}")
        except FoxgloveError as exc:
            if _is_not_found(exc):
                return None
            raise
        if not data:
            return None
        return FoxgloveRecording.from_api(data)

    def find_recording(
        self,
        recording: str,
        device: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = 200,
    ) -> list[FoxgloveRecording]:
        """Resolve a user-supplied identifier to matching recordings.

        Accepts a recording id, a key, or a file name as shown in Foxglove.
        Returns every match so an ambiguous name can be reported rather than
        silently guessing.
        """
        exact = self.get_recording(recording)
        if exact is not None:
            return [exact]

        wanted = Path(recording).name
        wanted_stem = _mcap_stem(wanted)
        candidates = self.list_recordings(
            device=device, start=start, end=end, limit=limit
        )
        matches = [
            rec
            for rec in candidates
            if wanted in (Path(rec.path).name, rec.key, rec.id)
            or _mcap_stem(Path(rec.path).name) == wanted_stem
            or _mcap_stem(rec.key) == wanted_stem
        ]
        return matches

    def request_import(self, key_or_id: str) -> str:
        """Ask Foxglove to import a recording from its device / edge site.

        Returns the resulting import status. The call is idempotent: a recording
        that is already imported or already queued returns 200.
        """
        quoted = urllib.parse.quote(key_or_id, safe="")
        data = self._request_json("POST", f"/v1/recordings/{quoted}/import")
        if isinstance(data, dict) and data.get("importStatus"):
            return str(data["importStatus"])
        return "pending"

    def wait_for_import(
        self,
        key_or_id: str,
        timeout_s: int = 900,
        poll_interval_s: float = 5.0,
        sleep=time.sleep,
    ) -> FoxgloveRecording:
        """Poll until the recording is imported, or raise on failure/timeout."""
        deadline = time.monotonic() + timeout_s
        last_status = ""
        while True:
            recording = self.get_recording(key_or_id)
            if recording is None:
                raise FoxgloveError(f"Recording {key_or_id!r} disappeared while importing")
            last_status = recording.import_status
            if recording.is_imported:
                return recording
            if last_status == IMPORT_FAILED:
                raise FoxgloveError(
                    f"Foxglove reported import failure for recording {key_or_id!r}. "
                    "Check the device connection and the edge site status in Foxglove."
                )
            if time.monotonic() >= deadline:
                raise FoxgloveError(
                    f"Timed out after {timeout_s}s waiting for recording {key_or_id!r} "
                    f"to upload from the device (last status: {last_status or 'unknown'}). "
                    "The upload continues in the background — retry this tool later."
                )
            sleep(poll_interval_s)

    # -- download ----------------------------------------------------------

    def download_recording(
        self,
        recording: FoxgloveRecording,
        dest: Path,
        topics: Iterable[str] | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> int:
        """Download a recording as MCAP to ``dest``. Returns bytes written."""
        body: dict[str, Any] = {"recordingId": recording.id, "outputFormat": "mcap"}
        if recording.key:
            body["key"] = recording.key
        if topics:
            body["topics"] = list(topics)
        if start:
            body["start"] = start
        if end:
            body["end"] = end

        last_error: FoxgloveError | None = None
        for path in _DOWNLOAD_PATHS:
            try:
                return self._download_via(path, body, dest)
            except FoxgloveError as exc:
                if not _is_not_found(exc):
                    raise
                last_error = exc
        raise last_error or FoxgloveError("No usable Foxglove download endpoint")

    def _download_via(self, path: str, body: dict[str, Any], dest: Path) -> int:
        """POST to a download endpoint and stream the result into ``dest``.

        The endpoint either returns the MCAP bytes directly or a JSON document
        with a short-lived signed ``link`` to fetch them from.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")

        with self._open("POST", path, body=body, accept="application/octet-stream") as response:
            content_type = (response.headers.get("Content-Type") or "").lower()
            if "application/json" in content_type:
                payload = json.loads(response.read() or b"{}")
                link = payload.get("link") or payload.get("url")
                if not link:
                    raise FoxgloveError(
                        f"Foxglove {path} returned no download link for the recording"
                    )
            else:
                link = None
                written = _stream_to_file(response, tmp)

        if link is not None:
            written = self._download_link(link, tmp)

        if written == 0:
            tmp.unlink(missing_ok=True)
            raise FoxgloveError(
                "Foxglove returned an empty file — the recording may contain no "
                "messages in the requested topic/time range."
            )
        tmp.replace(dest)
        return written

    def _download_link(self, link: str, dest: Path) -> int:
        request = urllib.request.Request(link, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
                return _stream_to_file(response, dest)
        except urllib.error.HTTPError as exc:
            raise _http_error(exc, "GET", link) from exc
        except urllib.error.URLError as exc:
            raise FoxgloveError(f"Could not download recording data: {exc.reason}") from exc


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _api_key_from_env() -> str:
    for name in _API_KEY_ENV_VARS:
        value = os.environ.get(name)
        if value:
            return value
    return ""


def _looks_like_id(value: str) -> bool:
    """Foxglove ids are prefixed slugs such as ``dev_1a2b3c`` or ``rec_...``."""
    return "_" in value and " " not in value and value.split("_", 1)[0].isalpha()


def _mcap_stem(value: str) -> str:
    name = Path(value).name
    return name[: -len(".mcap")] if name.endswith(".mcap") else name


def _stream_to_file(response, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        shutil.copyfileobj(response, f, length=1024 * 1024)
    return dest.stat().st_size


def _http_error(exc: urllib.error.HTTPError, method: str, url: str) -> FoxgloveError:
    detail = ""
    try:
        raw = exc.read()
        if raw:
            parsed = json.loads(raw)
            detail = parsed.get("error") or parsed.get("message") or raw.decode(errors="replace")
    except Exception:  # noqa: BLE001 - error bodies are best effort
        detail = ""
    suffix = f": {detail}" if detail else ""
    if exc.code in (401, 403):
        return FoxgloveAuthError(
            f"Foxglove rejected the API key ({exc.code}){suffix}. Check FOXGLOVE_API_KEY "
            "and that the key has recording read/import permissions."
        )
    return FoxgloveError(f"Foxglove API {method} {url} failed with HTTP {exc.code}{suffix}")


def _is_not_found(exc: FoxgloveError) -> bool:
    message = str(exc)
    return "HTTP 404" in message or "HTTP 405" in message
