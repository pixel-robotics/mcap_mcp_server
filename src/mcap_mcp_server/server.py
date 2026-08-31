"""MCP server: tool and resource registration for MCAP querying."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any

import pandas as pd
from fastmcp import Context, FastMCP

from mcap_mcp_server import __version__
from mcap_mcp_server.auth import build_auth
from mcap_mcp_server.config import ServerConfig
from mcap_mcp_server.decoder_registry import DecoderRegistry
from mcap_mcp_server.foxglove import FoxgloveClient, FoxgloveError, FoxgloveRecording
from mcap_mcp_server.mcap_reader import (
    get_schema_info,
    get_summary,
    topic_to_table_name,
)
from mcap_mcp_server.query_engine import QueryEngine
from mcap_mcp_server.recording_index import RecordingIndex, _ns_to_iso

logger = logging.getLogger(__name__)


def create_server(config: ServerConfig) -> FastMCP:
    """Build and return a fully configured MCP server instance."""
    mcp = FastMCP(
        name="mcap-mcp-server",
        instructions=(
            "This server provides SQL query access to MCAP robotics recording files. "
            "The easiest entry point is load_interval: give it a start/end time (and "
            "a device name), and it finds every matching recording — fetching it "
            "from Foxglove and triggering the upload from the robot first when "
            "needed — loads the data into DuckDB, and returns the tables. Then run "
            "SQL with the query tool. All tables have a timestamp_us (BIGINT, "
            "microseconds) column for time-based JOINs across topics. The "
            "finer-grained tools (list_recordings, get_schema, load_recording, "
            "list_foxglove_recordings, import_foxglove_recording) remain available "
            "for step-by-step control."
        ),
        auth=build_auth(config),
    )

    registry = DecoderRegistry(flatten_depth=config.flatten_depth)
    registry.discover()

    index = RecordingIndex(recursive=config.recursive)

    foxglove = FoxgloveClient(
        api_key=config.foxglove_api_key or None,
        api_url=config.foxglove_api_url,
    )

    engine = QueryEngine(
        query_timeout_s=config.query_timeout_s,
        default_row_limit=config.default_row_limit,
        max_row_limit=config.max_row_limit,
        max_memory_mb=config.max_memory_mb,
    )

    # Serializes DuckDB table registration across concurrently running loads.
    load_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    @mcp.tool(
        name="list_recordings",
        description=(
            "Discover available MCAP recording files. Does not require loading. "
            "Returns file names, sizes, durations, channel lists, and message counts. "
            "Use this first to see what data is available before loading. "
            "By default scans the project directory; pass an absolute 'path' to "
            "scan any directory on the filesystem."
        ),
    )
    def list_recordings(
        path: str | None = None,
        after: str | None = None,
        before: str | None = None,
    ) -> str:
        """List MCAP recordings in the data directory."""
        scan_path = Path(path) if path else config.data_dir
        after_dt = _parse_datetime(after)
        before_dt = _parse_datetime(before)
        summaries = index.scan(scan_path, after=after_dt, before=before_dt)
        return json.dumps(index.to_json(summaries), indent=2)

    @mcp.tool(
        name="get_recording_info",
        description=(
            "Get full metadata, channel details, and attachment list for a "
            "specific MCAP recording file. Does not require loading. "
            "Use this for detailed inspection before loading data."
        ),
    )
    def get_recording_info(file: str) -> str:
        """Return detailed recording metadata, channels, and attachments."""
        file_path = _resolve_file(file, config.data_dir)
        s = get_summary(file_path)

        channels: dict[str, Any] = {}
        for ch in s.channels:
            channels[ch.topic] = {
                "schema_name": ch.schema_name,
                "message_encoding": ch.message_encoding,
                "message_count": ch.message_count,
            }

        result: dict[str, Any] = {
            "file": Path(s.path).name,
            "path": s.path,
            "size_mb": round(s.size_mb, 1),
            "library": s.library,
            "start_time": _ns_to_iso(s.start_time_ns),
            "end_time": _ns_to_iso(s.end_time_ns),
            "duration_s": round(s.duration_s, 1),
            "message_count": s.message_count,
            "channels": channels,
            "metadata": s.metadata,
            "attachments": s.attachment_names,
        }
        return json.dumps(result, indent=2)

    @mcp.tool(
        name="get_schema",
        description=(
            "Inspect the SQL schema for a recording: topic names, table names, "
            "column names and DuckDB types. Does not require loading. "
            "Use this to plan SQL queries before running them. "
            "Returns a sql_hint with JOIN guidance."
        ),
    )
    def get_schema(
        file: str,
        topic: str | None = None,
    ) -> str:
        """Get schema info for SQL query planning."""
        file_path = _resolve_file(file, config.data_dir)
        topics_info = get_schema_info(file_path, registry, topic=topic)

        result: dict[str, Any] = {
            "file": Path(file_path).name,
            "topics": {},
            "metadata_table": "_metadata",
            "sql_hint": (
                "Tables are named from topics: strip leading '/', replace '/' "
                "with '_'. All tables have a 'timestamp_us' column (BIGINT, "
                "microseconds). Use it for JOINs across topics. DuckDB supports "
                "ASOF JOIN for time-series with different sample rates."
            ),
        }
        for topic_name, schema in topics_info.items():
            result["topics"][topic_name] = {
                "table_name": schema.table_name,
                "message_count": schema.message_count,
                "schema_name": schema.schema_name,
                "message_encoding": schema.message_encoding,
                "fields": [
                    {"name": f.name, "type": f.type, "description": f.description}
                    for f in schema.fields
                ],
            }
        return json.dumps(result, indent=2)

    @mcp.tool(
        name="load_recording",
        description=(
            "Decode an MCAP file and load its data into DuckDB for SQL querying. "
            "This decodes all messages and may take seconds to tens of seconds "
            "depending on file size. You must call this before running queries. "
            "For large files, use 'topics' to load only the topics you need and "
            "'start_time'/'end_time' to narrow the time window — this significantly "
            "reduces both load time and memory usage. "
            "Set an alias for multi-recording comparison."
        ),
    )
    def load_recording(
        file: str,
        alias: str | None = None,
        topics: list[str] | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        downsample: int | None = None,
    ) -> str:
        """Load MCAP data into DuckDB tables."""
        file_path = _resolve_file(file, config.data_dir)
        result = _load_file(
            file_path,
            alias=alias,
            topics=topics,
            start_time=start_time,
            end_time=end_time,
            downsample=downsample,
        )
        return json.dumps(result, indent=2)

    def _load_file(
        file_path: Path,
        alias: str | None = None,
        topics: list[str] | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        downsample: int | None = None,
    ) -> dict[str, Any]:
        """Decode an MCAP file into DuckDB tables; shared by the load tools."""
        summary = get_summary(file_path)

        start_ns = _parse_time_to_ns(start_time)
        end_ns = _parse_time_to_ns(end_time)

        decodable_channels: dict[int, dict] = {}
        skipped_topics: list[str] = []

        for ch in summary.channels:
            if topics and ch.topic not in topics:
                continue
            decoder = registry.get_decoder(ch.message_encoding, ch.schema_encoding)
            if decoder is None:
                skipped_topics.append(ch.topic)
                continue
            decodable_channels[ch.channel_id] = {
                "topic": ch.topic,
                "decoder": decoder,
                "schema_id": ch.schema_id,
                "schema_name": ch.schema_name,
                "schema_encoding": ch.schema_encoding,
                "message_encoding": ch.message_encoding,
            }

        # Accumulate decoded messages per topic
        topic_columns: dict[str, dict[str, list]] = {}
        topic_field_names: dict[str, list[str] | None] = {}
        decode_errors: dict[str, int] = {}

        load_start = time.monotonic()
        msg_count = 0

        with open(file_path, "rb") as f:
            from mcap.reader import make_reader

            reader = make_reader(f)
            file_summary = reader.get_summary()
            schemas_by_id = file_summary.schemas if file_summary else {}

            topic_list = [info["topic"] for info in decodable_channels.values()] or None

            for schema_rec, channel, message in reader.iter_messages(
                topics=topic_list,
                start_time=start_ns,
                end_time=end_ns,
                log_time_order=True,
            ):
                if channel.id not in decodable_channels:
                    continue

                msg_count += 1
                if downsample and msg_count % downsample != 0:
                    continue

                info = decodable_channels[channel.id]
                decoder = info["decoder"]
                topic = info["topic"]

                schema_data = b""
                if schema_rec is not None:
                    schema_data = schema_rec.data
                elif info["schema_id"] in schemas_by_id:
                    schema_data = schemas_by_id[info["schema_id"]].data

                try:
                    decoded = decoder.decode(
                        schema_data,
                        message.data,
                        schema_name=info["schema_name"],
                        schema_encoding=info["schema_encoding"],
                        schema_id=info["schema_id"],
                    )
                except Exception:
                    logger.debug("Failed to decode message on %s", topic, exc_info=True)
                    decode_errors[topic] = decode_errors.get(topic, 0) + 1
                    continue

                if topic not in topic_columns:
                    topic_columns[topic] = {"timestamp_us": []}
                    topic_field_names[topic] = None

                cols = topic_columns[topic]
                cols["timestamp_us"].append(message.log_time // 1000)

                if topic_field_names[topic] is None:
                    topic_field_names[topic] = list(decoded.keys())
                    for field_name in decoded:
                        cols[field_name] = []

                for field_name in topic_field_names[topic]:  # type: ignore[union-attr]
                    cols.setdefault(field_name, []).append(decoded.get(field_name))

        tables_info: dict[str, dict[str, int]] = {}
        total_rows = 0
        total_memory_bytes = 0
        load_group = alias or Path(file_path).name

        # Loads can run concurrently (several users, or a client retrying while
        # an earlier long load is still finishing) — registration and the
        # read-modify-write of _recordings must not interleave.
        with load_lock:
            engine.drain_evicted()

            for topic, cols in topic_columns.items():
                table_name = topic_to_table_name(topic, alias)
                df = pd.DataFrame(cols)
                total_memory_bytes += int(df.memory_usage(deep=True).sum())
                row_count = engine.register_dataframe(table_name, df, group=load_group)
                tables_info[table_name] = {"rows": row_count, "columns": len(df.columns)}
                total_rows += row_count

            _register_metadata_table(engine, summary, alias, group=load_group)

            if alias:
                _register_recordings_entry(engine, summary, alias)

            evicted = engine.drain_evicted()

        load_time = time.monotonic() - load_start
        memory_budget_mb = config.max_memory_mb
        memory_used_mb = round(engine.total_memory_bytes / (1024 * 1024), 1)

        result: dict[str, Any] = {
            "status": "loaded",
            "file": Path(file_path).name,
            "alias": alias,
            "tables": tables_info,
            "skipped_topics": skipped_topics,
            "skipped_reason": "no decoder available or binary blob" if skipped_topics else None,
            "total_rows": total_rows,
            "memory_mb": round(total_memory_bytes / (1024 * 1024), 1),
            "memory_used_mb": memory_used_mb,
            "memory_budget_mb": memory_budget_mb,
            "load_time_s": round(load_time, 1),
        }
        if evicted:
            result["evicted_tables"] = sorted(set(evicted))
            result["eviction_warning"] = (
                "Memory budget exceeded. Previously loaded tables were evicted "
                "to make room. Use topic and time filters to reduce memory usage."
            )
        if decode_errors:
            result["decode_errors"] = decode_errors
            result["decode_error_hint"] = (
                "Messages on these topics failed to decode and were dropped "
                "(count per topic). A topic where every message failed gets no "
                "table at all — that indicates a decoder bug worth reporting."
            )
        return result

    @mcp.tool(
        name="query",
        description=(
            "Execute a SQL query against loaded MCAP data. Supports full DuckDB SQL "
            "including JOINs, GROUP BY, window functions, and ASOF JOIN for "
            "time-series correlation. Data must be loaded first via load_recording. "
            "If a table is missing, call load_recording with the needed topic."
        ),
    )
    def query(
        sql: str,
        limit: int | None = None,
    ) -> str:
        """Run a SQL query on loaded data."""
        try:
            result = engine.execute(sql, limit=limit)
        except ValueError as e:
            result = {"error": str(e)}

        if "error" in result and "does not exist" in str(result["error"]):
            loaded = engine.list_tables()
            result["loaded_tables"] = list(loaded.keys()) if loaded else []
            result["hint"] = (
                "Table not found. Call load_recording to load the needed topic. "
                "Use get_schema to see available topics in a file."
            )

        return json.dumps(result, default=_json_default, indent=2)

    @mcp.tool(
        name="list_foxglove_recordings",
        description=(
            "List recordings available in Foxglove, including ones still sitting "
            "on a robot or edge site that have not been uploaded yet. Each entry "
            "reports an import_status ('complete' means downloadable now; 'none', "
            "'pending' or 'importing' means it still has to be uploaded from the "
            "device) and whether a local copy already exists. "
            "Use this to discover recordings that list_recordings cannot see "
            "because they are not on this machine. Requires a Foxglove API key."
        ),
    )
    def list_foxglove_recordings(
        device: str | None = None,
        start: str | None = None,
        end: str | None = None,
        import_status: str | None = None,
        limit: int = 50,
    ) -> str:
        """List remote Foxglove recordings and whether they are local already."""
        try:
            recordings = foxglove.list_recordings(
                device=device,
                start=start,
                end=end,
                import_status=import_status,
                limit=limit,
            )
            entries = []
            for rec in recordings:
                entry = rec.to_json()
                local = _find_local_recording(rec.filename, config)
                entry["local_path"] = str(local) if local else None
                entry["needs_device_upload"] = not rec.is_imported
                entries.append(entry)
        except FoxgloveError as e:
            return json.dumps({"error": str(e)}, indent=2)

        return json.dumps(
            {
                "count": len(entries),
                "recordings": entries,
                "hint": (
                    "Call import_foxglove_recording with the recording id, key or "
                    "file name to download it. Recordings with "
                    "needs_device_upload=true are uploaded from the device first, "
                    "which can take several minutes."
                ),
            },
            indent=2,
        )

    @mcp.tool(
        name="import_foxglove_recording",
        description=(
            "Make a Foxglove recording available locally as an MCAP file, then "
            "return its path so it can be loaded with load_recording. "
            "If the file is already on this machine it is returned immediately. "
            "Otherwise it is downloaded from Foxglove — and if the recording is "
            "still on the robot or edge site (import_status other than 'complete') "
            "the upload from the device is triggered automatically and waited for. "
            "Identify the recording by id, key, or file name; alternatively give a "
            "device plus a start/end time window. Requires a Foxglove API key."
        ),
    )
    def import_foxglove_recording(
        recording: str | None = None,
        device: str | None = None,
        start: str | None = None,
        end: str | None = None,
        topics: list[str] | None = None,
        force: bool = False,
        wait: bool = True,
        timeout_s: int | None = None,
    ) -> str:
        """Download a Foxglove recording, uploading it from the device if needed."""
        if not recording and not device:
            return json.dumps(
                {
                    "error": (
                        "Specify 'recording' (id, key or file name) or 'device' "
                        "together with a start/end time window."
                    )
                },
                indent=2,
            )

        # A topic or time filter produces a partial file, so it gets its own
        # name and never satisfies (or is satisfied by) a full-recording import.
        filtered = bool(topics or start or end)

        # 1. Already on disk? Nothing to import.
        if recording and not force and not filtered:
            local = _find_local_recording(recording, config)
            if local is not None:
                return json.dumps(
                    {
                        "status": "already_local",
                        "path": str(local),
                        "size_mb": round(local.stat().st_size / (1024 * 1024), 1),
                        "hint": "Call load_recording with this path to query it.",
                    },
                    indent=2,
                )

        try:
            # 2. Resolve which remote recording is meant.
            match = _resolve_foxglove_recording(
                foxglove, recording, device, start, end
            )
            if isinstance(match, dict):
                return json.dumps(match, indent=2)

            result = _materialize(
                match,
                topics=topics,
                start=start,
                end=end,
                force=force,
                wait=wait,
                timeout_s=timeout_s,
            )
        except FoxgloveError as e:
            return json.dumps({"error": str(e)}, indent=2)

        if result["status"] == "import_started":
            result["hint"] = (
                "The device is uploading the recording. Call this tool again "
                "in a few minutes to download it."
            )
        else:
            result["hint"] = "Call load_recording with this path to query it."
        return json.dumps(result, indent=2)

    def _materialize(
        match: FoxgloveRecording,
        topics: list[str] | None = None,
        start: str | None = None,
        end: str | None = None,
        force: bool = False,
        wait: bool = True,
        timeout_s: int | None = None,
    ) -> dict[str, Any]:
        """Turn a remote Foxglove recording into a local MCAP file.

        Triggers the upload from the device when the recording has not been
        imported yet, then downloads it. Raises FoxgloveError on failure.
        """
        # Still on the device? Ask Foxglove to pull it in.
        import_triggered = False
        import_wait_s = 0.0
        if not match.is_imported:
            import_status = foxglove.request_import(match.id or match.key)
            import_triggered = True
            logger.info(
                "Requested Foxglove import of %s (status: %s)",
                match.id,
                import_status,
            )
            if not wait:
                return {
                    "status": "import_started",
                    "recording": match.to_json(),
                    "import_status": import_status,
                }
            wait_start = time.monotonic()
            match = foxglove.wait_for_import(
                match.id or match.key,
                timeout_s=timeout_s or config.foxglove_import_timeout_s,
            )
            import_wait_s = time.monotonic() - wait_start

        # Download the MCAP bytes.
        dest = config.foxglove_dir / _import_filename(match, topics, start, end)
        if dest.exists() and not force:
            downloaded_bytes = dest.stat().st_size
            status = "already_local"
        else:
            download_start = time.monotonic()
            downloaded_bytes = foxglove.download_recording(
                match, dest, topics=topics, start=start, end=end
            )
            logger.info(
                "Downloaded %s (%.1f MB) in %.1fs",
                dest,
                downloaded_bytes / (1024 * 1024),
                time.monotonic() - download_start,
            )
            status = "imported"

        index.invalidate()

        return {
            "status": status,
            "path": str(dest),
            "file": dest.name,
            "size_mb": round(downloaded_bytes / (1024 * 1024), 1),
            "recording": match.to_json(),
            "uploaded_from_device": import_triggered,
            "device_upload_wait_s": round(import_wait_s, 1),
        }

    @mcp.tool(
        name="load_interval",
        description=(
            "One-stop tool: make all recorded data for a time interval queryable "
            "with SQL. Give start and end (ISO 8601) and usually a device name. "
            "The tool finds every recording overlapping the interval, fetches "
            "missing ones from Foxglove — triggering the upload from the robot "
            "first when a recording is still on the device, which can take "
            "several minutes — loads the data (restricted to the interval) into "
            "DuckDB, and returns the resulting tables. Afterwards run SQL with "
            "the query tool. Progress is reported while it runs; intervals "
            "longer than a couple of minutes span many recordings, so pass "
            "'topics' to load only what you need — that is much faster than "
            "loading hundreds of topics per recording. Prefer this over the "
            "individual list/import/load tools unless you need fine-grained "
            "control."
        ),
    )
    async def load_interval(
        start: str,
        end: str,
        device: str | None = None,
        topics: list[str] | None = None,
        downsample: int | None = None,
        ctx: Context | None = None,
    ) -> str:
        """Find, fetch, and load every recording overlapping [start, end]."""
        interval = _normalize_interval(start, end)
        if isinstance(interval, dict):
            return json.dumps(interval, indent=2)
        start_iso, end_iso = interval

        async def progress(done: float, total: float | None, message: str) -> None:
            if ctx is None:
                return
            try:
                await ctx.report_progress(progress=done, total=total, message=message)
            except Exception:  # progress is best-effort
                logger.debug("Could not send progress notification", exc_info=True)

        async def run_blocking(func, done: float, total: float | None, message: str):
            """Run a blocking step in a worker thread, heartbeating progress.

            The heartbeat keeps the MCP connection active so clients that reset
            their request timeout on progress notifications don't give up on a
            long device upload or a big load.
            """
            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(None, func)
            while True:
                try:
                    return await asyncio.wait_for(asyncio.shield(future), timeout=10)
                except asyncio.TimeoutError:
                    await progress(done, total, message)

        notes: list[str] = []
        sources: list[dict[str, Any]] = []
        paths: list[Path] = []

        # 1. Ask Foxglove which recordings overlap the interval.
        remote: list[FoxgloveRecording] = []
        if foxglove.configured:
            try:
                remote = await run_blocking(
                    partial(
                        foxglove.list_recordings,
                        device=device,
                        start=start_iso,
                        end=end_iso,
                        limit=200,
                    ),
                    0, None, "listing Foxglove recordings",
                )
            except FoxgloveError as e:
                notes.append(f"Foxglove lookup failed, falling back to local files: {e}")
        else:
            notes.append(
                "No Foxglove API key configured — only recordings already on "
                "this machine were considered."
            )

        if device is None and remote:
            devices = sorted(
                {rec.device_name or rec.device_id for rec in remote} - {""}
            )
            if len(devices) > 1:
                return json.dumps(
                    {
                        "error": (
                            f"Recordings from {len(devices)} devices overlap "
                            "this interval. Pass 'device' to pick one."
                        ),
                        "devices": devices,
                    },
                    indent=2,
                )

        # Fetching and loading each count as one progress step.
        total_steps = 2 * len(remote) if remote else None

        # 2. Make each remote recording local. Uploads from the devices are
        # triggered for all of them up front so they run concurrently and the
        # waits below overlap instead of adding up.
        def trigger_imports() -> None:
            for rec in remote:
                if not rec.is_imported:
                    try:
                        foxglove.request_import(rec.id or rec.key)
                    except FoxgloveError as e:
                        logger.warning("Could not trigger import of %s: %s", rec.id, e)

        if remote:
            await run_blocking(
                trigger_imports, 0, total_steps, "triggering uploads from devices"
            )

        deadline = time.monotonic() + config.foxglove_import_timeout_s
        for i, rec in enumerate(remote):
            message = f"fetching recording {i + 1}/{len(remote)} ({rec.filename})"
            await progress(i, total_steps, message)
            try:
                remaining = max(30, int(deadline - time.monotonic()))
                fetched = await run_blocking(
                    partial(_materialize, rec, wait=True, timeout_s=remaining),
                    i, total_steps, message,
                )
                sources.append(fetched)
                paths.append(Path(fetched["path"]))
            except FoxgloveError as e:
                sources.append(
                    {"status": "error", "recording": rec.to_json(), "error": str(e)}
                )

        # 3. No Foxglove results? Fall back to what is already on disk.
        if not paths:
            after_dt = _parse_datetime(start_iso)
            before_dt = _parse_datetime(end_iso)
            for summary in await run_blocking(
                partial(index.scan, config.data_dir, after=after_dt, before=before_dt),
                0, None, "scanning local recordings",
            ):
                paths.append(Path(summary.path))
                sources.append({"status": "already_local", "path": summary.path})
            total_steps = len(paths)

        if not paths:
            return json.dumps(
                {
                    "error": "No recordings overlap this interval.",
                    "interval": {"start": start_iso, "end": end_iso},
                    "device": device,
                    "recordings": sources,
                    "notes": notes,
                    "hint": (
                        "Check the device name and time window with "
                        "list_foxglove_recordings or list_recordings."
                    ),
                },
                indent=2,
            )

        # 4. Load everything into DuckDB, restricted to the interval.
        use_alias = len(paths) > 1
        tables: dict[str, Any] = {}
        skipped: set[str] = set()
        evicted: set[str] = set()
        total_rows = 0
        aliases: list[str] = []
        topic_tables: set[str] = set()
        decode_errors: dict[str, int] = {}
        fetch_steps = len(remote)
        for i, path in enumerate(paths):
            alias = _alias_from_path(path) if use_alias else None
            message = f"loading recording {i + 1}/{len(paths)} into DuckDB ({path.name})"
            await progress(fetch_steps + i, total_steps, message)
            try:
                loaded = await run_blocking(
                    partial(
                        _load_file,
                        path,
                        alias=alias,
                        topics=topics,
                        start_time=start_iso,
                        end_time=end_iso,
                        downsample=downsample,
                    ),
                    fetch_steps + i, total_steps, message,
                )
            except Exception as e:  # one bad file must not kill the rest
                logger.warning("Failed to load %s", path, exc_info=True)
                notes.append(f"failed to load {path.name}: {e}")
                continue
            tables.update(loaded["tables"])
            skipped.update(loaded["skipped_topics"])
            evicted.update(loaded.get("evicted_tables", []))
            total_rows += loaded["total_rows"]
            for topic, count in loaded.get("decode_errors", {}).items():
                decode_errors[topic] = decode_errors.get(topic, 0) + count
            if alias:
                aliases.append(alias)
            for table_name in loaded["tables"]:
                prefix = f"{alias}_" if alias else ""
                topic_tables.add(table_name.removeprefix(prefix))

        await progress(total_steps or 1, total_steps, "done")

        # A wide interval times many topics yields thousands of tables; the
        # per-table dict would dwarf the client's context window, so report the
        # naming scheme instead.
        tables_json: dict[str, Any]
        if len(tables) > 150:
            tables_json = {
                "table_count": len(tables),
                "recording_prefixes": aliases,
                "topic_tables": sorted(topic_tables),
                "naming": (
                    "Tables are named <recording_prefix>_<topic_table>. Query "
                    "one recording's table directly, or UNION across prefixes."
                ),
            }
        else:
            tables_json = tables

        result: dict[str, Any] = {
            "status": "loaded" if total_rows else "loaded_empty",
            "interval": {"start": start_iso, "end": end_iso},
            "device": device,
            "recordings": sources,
            "tables": tables_json,
            "skipped_topics": sorted(skipped),
            "total_rows": total_rows,
            "memory_used_mb": round(engine.total_memory_bytes / (1024 * 1024), 1),
            "memory_budget_mb": config.max_memory_mb,
            "hint": (
                "Data is loaded — run SQL with the query tool. Every table has "
                "a timestamp_us column (BIGINT, microseconds) for JOINs; use "
                "get_schema for column details."
            ),
        }
        if total_rows == 0:
            result["hint"] = (
                "Recordings were found but contained no decodable messages in "
                "this interval (or all topics were filtered out)."
            )
        if notes:
            result["notes"] = notes
        if evicted:
            result["evicted_tables"] = sorted(evicted)
            result["eviction_warning"] = (
                "Memory budget exceeded. Previously loaded tables were evicted "
                "to make room. Use topic filters or a narrower interval."
            )
        if decode_errors:
            result["decode_errors"] = decode_errors
            result["decode_error_hint"] = (
                "Messages on these topics failed to decode and were dropped "
                "(count per topic, summed over recordings)."
            )
        return json.dumps(result, indent=2)

    @mcp.tool(
        name="get_version",
        description=(
            "Return the server version, supported encodings, and upgrade command. "
            "Use this to check for updates or diagnose compatibility issues."
        ),
    )
    def get_version() -> str:
        """Return version info and available decoders."""
        result = {
            "version": __version__,
            "decoders": registry.available_encodings,
            "upgrade": "uvx mcap-mcp-server[all] --upgrade",
        }
        return json.dumps(result, indent=2)

    return mcp


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _resolve_file(file: str, data_dir: Path) -> Path:
    """Resolve a filename or path to an absolute Path."""
    p = Path(file)
    if p.is_absolute() and p.is_file():
        return p
    candidate = data_dir / file
    if candidate.is_file():
        return candidate
    # Try searching recursively
    for match in data_dir.rglob(Path(file).name):
        if match.is_file():
            return match
    raise FileNotFoundError(f"MCAP file not found: {file} (searched in {data_dir})")


def _find_local_recording(name: str, config: ServerConfig) -> Path | None:
    """Return an existing local MCAP file for a recording name, or None.

    Looks in the data directory and in the Foxglove download directory, both
    non-recursively and recursively, so a recording imported earlier is not
    downloaded a second time.
    """
    filename = Path(name).name
    if not filename:
        return None
    if not filename.endswith(".mcap"):
        filename = f"{filename}.mcap"

    direct = Path(name)
    if direct.is_absolute() and direct.is_file():
        return direct

    for root in (config.foxglove_dir, config.data_dir):
        candidate = root / filename
        if candidate.is_file():
            return candidate

    try:
        for match in config.data_dir.rglob(filename):
            if match.is_file():
                return match
    except OSError:
        logger.debug("Could not scan %s for %s", config.data_dir, filename)
    return None


def _import_filename(
    recording: FoxgloveRecording,
    topics: list[str] | None,
    start: str | None,
    end: str | None,
) -> str:
    """Local file name for an imported recording.

    A topic or time filter yields only part of the recording, so it gets a
    name of its own — otherwise a later full import would be silently served
    from the truncated file.
    """
    name = recording.filename
    if not (topics or start or end):
        return name

    fingerprint = json.dumps(
        {"topics": sorted(topics or []), "start": start, "end": end},
        sort_keys=True,
    )
    digest = hashlib.sha1(fingerprint.encode()).hexdigest()[:8]
    return f"{name[: -len('.mcap')]}_part-{digest}.mcap"


def _resolve_foxglove_recording(
    client: FoxgloveClient,
    recording: str | None,
    device: str | None,
    start: str | None,
    end: str | None,
) -> FoxgloveRecording | dict[str, Any]:
    """Pick the single remote recording meant by the tool arguments.

    Returns a JSON-ready dict instead of a recording when nothing matched or
    the arguments are ambiguous — guessing between recordings would download
    the wrong data.
    """
    if recording:
        matches = client.find_recording(recording, device=device, start=start, end=end)
        subject = f"recording {recording!r}"
    else:
        matches = client.list_recordings(device=device, start=start, end=end, limit=50)
        subject = f"device {device!r} between {start!r} and {end!r}"

    if not matches:
        return {
            "error": f"No Foxglove recording found for {subject}.",
            "hint": (
                "Use list_foxglove_recordings to see what is available, and check "
                "the device name and time window."
            ),
        }

    if len(matches) > 1:
        return {
            "error": f"{len(matches)} Foxglove recordings match {subject}.",
            "candidates": [rec.to_json() for rec in matches[:20]],
            "hint": "Call again with 'recording' set to one of the ids above.",
        }

    return matches[0]


def _normalize_interval(start: str, end: str) -> tuple[str, str] | dict[str, str]:
    """Parse and validate an interval, returning UTC ISO strings or an error dict.

    Naive timestamps are taken as UTC; the returned strings are RFC 3339 with a
    'Z' suffix, the form the Foxglove API expects.
    """
    start_dt = _parse_datetime(start)
    end_dt = _parse_datetime(end)
    if start_dt is None or end_dt is None:
        return {
            "error": (
                "start and end must be ISO 8601 timestamps, "
                "e.g. 2026-08-30T14:00:00Z."
            )
        }
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=timezone.utc)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=timezone.utc)
    if end_dt <= start_dt:
        return {"error": "end must be after start."}

    def fmt(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    return fmt(start_dt), fmt(end_dt)


def _alias_from_path(path: Path) -> str:
    """SQL-safe table prefix derived from a file name."""
    stem = re.sub(r"[^A-Za-z0-9_]", "_", Path(path).stem).strip("_") or "rec"
    if stem[0].isdigit():
        stem = f"r_{stem}"
    return stem


def _normalize_iso(value: str) -> str:
    """Replace trailing 'Z' with '+00:00' for Python 3.10 fromisoformat compat."""
    if value.endswith("Z"):
        return value[:-1] + "+00:00"
    return value


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(_normalize_iso(value))
    except ValueError:
        return None


def _parse_time_to_ns(value: str | None) -> int | None:
    """Parse an ISO 8601 string or integer microseconds to nanoseconds."""
    if value is None:
        return None
    try:
        us = int(value)
        return us * 1000
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(_normalize_iso(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1e9)
    except ValueError:
        return None


def _register_metadata_table(
    engine: QueryEngine, summary: Any, alias: str | None, group: str = "_default"
) -> None:
    """Create the _metadata table from MCAP metadata records."""
    rows = []
    for record_name, kv in summary.metadata.items():
        for k, v in kv.items():
            rows.append({"record_name": record_name, "key": k, "value": v})
    if rows:
        table_name = f"{alias}__metadata" if alias else "_metadata"
        df = pd.DataFrame(rows)
        engine.register_dataframe(table_name, df, group=group)


def _register_recordings_entry(
    engine: QueryEngine, summary: Any, alias: str
) -> None:
    """Add an entry to the cross-recording _recordings table."""
    row = {
        "alias": alias,
        "file_path": summary.path,
        "start_time": summary.start_time_ns,
        "end_time": summary.end_time_ns,
        "duration_s": summary.duration_s,
        "message_count": summary.message_count,
        "channel_count": len(summary.channels),
    }
    df = pd.DataFrame([row])
    try:
        existing = engine.execute("SELECT * FROM _recordings", limit=10000)
        if "error" not in existing:
            engine.unregister("_recordings")
            old_df = pd.DataFrame(existing["rows"], columns=existing["columns"])
            df = pd.concat([old_df, df], ignore_index=True)
    except Exception:
        pass
    engine.register_dataframe("_recordings", df)


def _json_default(obj: Any) -> Any:
    """JSON serialiser fallback for types DuckDB may return."""
    import decimal

    if isinstance(obj, decimal.Decimal):
        return float(obj)
    if isinstance(obj, (bytes, bytearray)):
        return obj.hex()
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return str(obj)
