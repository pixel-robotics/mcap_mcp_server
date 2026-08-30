# MCP Tools

Eight tools are exposed via the Model Context Protocol. Typical workflow: `list_recordings` → `get_schema` → `load_recording` → `query`. When the recording is not on this machine yet, start with `list_foxglove_recordings` → `import_foxglove_recording` instead.

## list_recordings

Discover available MCAP files in the configured data directory.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `path` | string | No | Override scan directory (defaults to `MCAP_DATA_DIR`) |
| `after` | string | No | ISO 8601 datetime — only recordings after this time |
| `before` | string | No | ISO 8601 datetime — only recordings before this time |

Returns a JSON array of recording summaries: filename, size, duration, channel list, message counts, metadata keys.

## get_recording_info

Full metadata, channel details, and attachment list for a specific file. Does not require loading.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `file` | string | Yes | Filename or path to the MCAP file |

Returns file path, size, library, start/end times, duration, message count, per-channel details (schema, encoding, count), metadata records, and attachment names.

## get_schema

Inspect topics, table names, column names and DuckDB types before loading. Essential for query planning.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `file` | string | Yes | Filename or path to the MCAP file |
| `topic` | string | No | Filter to a single topic |

Returns per-topic field information with DuckDB types and a `sql_hint` explaining table naming and JOIN conventions.

## load_recording

Decode an MCAP file and register its data as DuckDB tables. Must be called before `query`.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `file` | string | Yes | Filename or path to the MCAP file |
| `alias` | string | No | Table prefix for multi-recording comparison (e.g. `"r1"`) |
| `topics` | string[] | No | Subset of topics to load (defaults to all decodable) |
| `start_time` | string | No | ISO 8601 or epoch microseconds — start of time window |
| `end_time` | string | No | ISO 8601 or epoch microseconds — end of time window |
| `downsample` | integer | No | Keep every Nth message |

Returns table names, row counts, column counts, load time, memory usage (`memory_used_mb`, `memory_budget_mb`), and any `evicted_tables` if the memory budget was exceeded. Topics without a matching decoder are skipped and listed.

### Table naming

Topics are mapped to table names by stripping the leading `/` and replacing `/` with `_`:

| Topic | Table name |
|-------|-----------|
| `/imu` | `imu` |
| `/battery/status` | `battery_status` |
| `/sensors/power` | `sensors_power` |

With an alias `"r1"`, tables become `r1_imu`, `r1_battery_status`, etc.

Every table includes a `timestamp_us` column (BIGINT, microseconds) derived from the MCAP message log time. Use it for cross-topic JOINs.

A `_metadata` table is always created with columns `(record_name, key, value)` containing all MCAP metadata records.

## query

Execute SQL against loaded data. Full DuckDB SQL is supported, including `ASOF JOIN`.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `sql` | string | Yes | SQL query |
| `limit` | integer | No | Override row limit (default: 1000, max: 10000) |

Returns columns, types, rows, row count, truncation flag, and execution time. If a referenced table does not exist, the error response includes `loaded_tables` and a `hint` to guide the LLM toward loading the correct recording first.

## list_foxglove_recordings

List recordings held by the [Foxglove Data Platform](https://docs.foxglove.dev/api), including recordings that are still sitting on a robot or edge site and have never been uploaded. Requires a Foxglove API key (see [Configuration](configuration.md)).

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `device` | string | No | Device name or device id (`dev_...`) |
| `start` | string | No | RFC 3339 timestamp — start of the time window |
| `end` | string | No | RFC 3339 timestamp — end of the time window |
| `import_status` | string | No | Filter: `none`, `pending`, `importing`, `failed`, `complete` |
| `limit` | integer | No | Max recordings to return (default 50, max 2000) |

Each entry carries the recording id, key, file name, device, time range, size, and:

- `import_status` — `complete` means the data is in Foxglove and can be downloaded now.
- `needs_device_upload` — `true` while the recording is still only on the device.
- `local_path` — set when a copy already exists on this machine, otherwise `null`.

## import_foxglove_recording

Make a Foxglove recording available locally as an MCAP file and return its path, ready for `load_recording`.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `recording` | string | No* | Recording id, key, or file name |
| `device` | string | No* | Device name or id — used with `start`/`end` instead of `recording` |
| `start` | string | No | RFC 3339 timestamp — start of the time window |
| `end` | string | No | RFC 3339 timestamp — end of the time window |
| `topics` | string[] | No | Download only these topics |
| `force` | boolean | No | Re-download even if a local copy exists (default `false`) |
| `wait` | boolean | No | Wait for a device upload to finish (default `true`) |
| `timeout_s` | integer | No | Override the upload wait timeout (default 900 s) |

\* Either `recording` or `device` must be given.

The tool resolves the request in four steps:

1. **Already local?** If a matching `.mcap` file exists in the data directory or the Foxglove download directory, its path is returned immediately with `status: "already_local"` — nothing is transferred.
2. **Resolve the recording.** The identifier is looked up by id, then by key, then by file name. If several recordings match, the tool returns the candidates rather than guessing, so the caller can pick one by id.
3. **Upload from the device.** If `import_status` is not `complete`, the recording only exists on the robot or edge site. The tool calls the Foxglove import endpoint to trigger the upload and then polls until it completes (or `timeout_s` is reached). Set `wait=false` to return right after triggering the upload and pick the file up on a later call.
4. **Download.** The imported MCAP is streamed into the Foxglove download directory (`<data_dir>/foxglove` by default) via a `.part` file, so an interrupted transfer never leaves a half-written recording behind. The local recording index is refreshed, so the file shows up in `list_recordings`.

Passing `topics`, `start` or `end` downloads only part of the recording, so the file is named `<recording>_part-<hash>.mcap`. A later full import therefore never gets served from a truncated file, and repeating the same filter reuses the file already fetched.

Successful responses contain `path`, `size_mb`, the resolved `recording`, `uploaded_from_device`, and `device_upload_wait_s`. Failures — no API key, an unknown recording, an upload that failed on the device side, a timeout — are returned as an `error` field with a hint, not raised.

```json
{
  "status": "imported",
  "path": "/data/recordings/foxglove/session_042.mcap",
  "file": "session_042.mcap",
  "size_mb": 128.4,
  "recording": {
    "id": "rec_a1b2c3",
    "import_status": "complete",
    "device": "pixel-bot-1"
  },
  "uploaded_from_device": true,
  "device_upload_wait_s": 74.2,
  "hint": "Call load_recording with this path to query it."
}
```

## get_version

Return the server version, list of available decoders, and the upgrade command.

No parameters. Returns:

```json
{
  "version": "0.5.1",
  "decoders": ["JsonDecoder", "ProtobufDecoder", "Ros1Decoder", "Ros2Decoder", "FlatBufferDecoder"],
  "upgrade": "uvx mcap-mcp-server[all] --upgrade"
}
```
