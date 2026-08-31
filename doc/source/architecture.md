# Architecture

## Data flow

```{mermaid}
graph TD
    Client["MCP Client"]
    Server["server.py<br/>FastMCP tools"]
    Index["recording_index.py<br/>Directory scanner"]
    Reader["mcap_reader.py<br/>Summary & iteration"]
    Registry["decoder_registry.py<br/>Encoding dispatch"]
    Decoders["decoders/<br/>JSON · Protobuf · ROS1 · ROS2 · FlatBuffer"]
    Engine["query_engine.py<br/>DuckDB wrapper"]
    Files["MCAP files on disk"]
    Foxglove["foxglove.py<br/>Data Platform client"]
    Cloud["Foxglove API<br/>+ robots / edge sites"]

    Client -->|"list_recordings<br/>get_recording_info<br/>get_schema<br/>get_version"| Server
    Client -->|"load_interval<br/>load_recording"| Server
    Client -->|query| Server
    Client -->|"list_foxglove_recordings<br/>import_foxglove_recording"| Server

    Server --> Foxglove
    Foxglove -->|"list · import from device · download"| Cloud
    Foxglove -->|"writes .mcap"| Files
    Server --> Index
    Server --> Reader
    Server --> Registry
    Server --> Engine

    Index --> Files
    Reader --> Files
    Registry --> Decoders
    Engine -->|"register DataFrame<br/>execute SQL<br/>LRU eviction"| Engine
```

## Module responsibilities

| Module | Role |
|--------|------|
| `server.py` | MCP tool registration (9 tools), request orchestration |
| `config.py` | Config loading: defaults → TOML → env vars → CLI. Validates `max_memory_mb >= 64` |
| `auth.py` | Google Workspace login for the HTTP transport: OAuth proxy in front of Google plus a token verifier that rejects accounts outside the allowed domains |
| `foxglove.py` | Foxglove Data Platform REST client: find recordings, trigger an upload from the device, download MCAP |
| `recording_index.py` | Scans directories for `.mcap` files, caches summaries, filters by date |
| `mcap_reader.py` | Reads MCAP summary and iterates messages using indexed reader |
| `decoder_registry.py` | Discovers and dispatches to the correct `MessageDecoder` by encoding |
| `decoders/base.py` | `MessageDecoder` protocol and type mappings |
| `decoders/*.py` | One decoder per encoding (JSON, Protobuf, ROS1, ROS2, FlatBuffer) |
| `query_engine.py` | DuckDB connection, table registration, SQL execution, LRU eviction, safety enforcement |
| `flatten.py` | Nested dict flattening for multi-level message schemas |

## Load path

1. `load_recording` receives a filename and optional topic/time filters
2. `mcap_reader.get_summary()` reads the MCAP summary section (end of file, fast)
3. For each channel, `decoder_registry` resolves the decoder by `(message_encoding, schema_encoding)`
4. `mcap_reader.iter_messages()` iterates messages using MCAP chunk indexes
5. Each message is decoded to a flat Python dict via the appropriate decoder
6. Dicts are accumulated into per-topic column lists, then converted to `pd.DataFrame`
7. `query_engine` registers each DataFrame as a named DuckDB table
8. If memory exceeds the configured budget, LRU eviction removes the oldest tables and reports them back to the caller
9. Subsequent `query` calls execute SQL against these tables

## Import path

1. `import_foxglove_recording` first checks the data directory and the Foxglove download directory — an existing local file short-circuits the whole path
2. The identifier is resolved against `GET /v1/recordings/{keyOrId}`, falling back to a filtered `GET /v1/recordings` listing matched on file name or key; more than one match is reported back instead of guessed
3. If `importStatus` is not `complete`, the recording is still on the robot or edge site: `POST /v1/recordings/{id}/import` asks Foxglove to pull it in, and the recording is then polled until it becomes `complete`
4. `POST /v1/data/download` (falling back to the older `/v1/data/stream`) returns either the MCAP bytes or a short-lived signed link, which is streamed to a `.part` file and renamed on success. A topic or time filter yields a partial recording, which is stored under a filter-specific name so it cannot be mistaken for the full file
5. The recording index is invalidated so the new file appears in `list_recordings`, and the path is handed back for `load_recording`

Only the standard library is used for HTTP, so the Foxglove tools add no dependencies.

`load_interval` composes the two paths: it lists every Foxglove recording overlapping the requested interval (falling back to the local index without an API key), triggers all pending device uploads up front so they run concurrently, materializes each recording exactly like the import path, and then runs the load path per file restricted to the interval — aliasing table names when more than one recording is loaded.

## Remote serving and authentication

With `MCAP_TRANSPORT=http` the same FastMCP server is exposed over streamable HTTP. `auth.py` fronts it with an OAuth proxy to Google (fastmcp's `GoogleProvider`): the server publishes the standard MCP OAuth discovery metadata, clients register dynamically and send the user through the Google login, and every request's token is verified against Google. A `WorkspaceTokenVerifier` layered on top rejects any verified-email domain not in `allowed_google_domains`, so authentication *and* authorization are enforced server-side. An HTTP server without a configured Google client refuses to start unless `MCAP_INSECURE_NO_AUTH=true` is set explicitly.

## Memory management

The query engine tracks approximate memory usage of registered tables. When `max_memory_mb` is exceeded, the least-recently-used tables are evicted. The `load_recording` response includes `memory_used_mb`, `memory_budget_mb`, and any `evicted_tables` so the LLM can adapt its strategy.

`max_memory_mb` must be at least 64 MB — lower values are rejected at config time.

## Query safety

- DuckDB runs in read-only mode
- File system functions (`read_csv`, `read_parquet`, `COPY`, `EXPORT`) are blocked
- Queries are subject to a configurable timeout (default 30s) and row limit (default 1000)
- When a query references an unloaded table, the error includes `loaded_tables` and a `hint` to guide the LLM toward loading the correct recording
