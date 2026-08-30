# Configuration

Configuration is layered: **defaults → TOML file → environment variables → CLI arguments**. Each layer overrides the previous.

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MCAP_DATA_DIR` | `.` | Root directory to scan for MCAP files |
| `MCAP_RECURSIVE` | `true` | Scan subdirectories |
| `MCAP_MAX_MEMORY_MB` | `2048` | Max memory for loaded data (minimum: 64) |
| `MCAP_QUERY_TIMEOUT_S` | `30` | SQL query timeout (seconds) |
| `MCAP_DEFAULT_ROW_LIMIT` | `1000` | Default result row limit |
| `MCAP_MAX_ROW_LIMIT` | `10000` | Maximum allowed row limit |
| `MCAP_LOG_LEVEL` | `INFO` | Log level |
| `MCAP_TRANSPORT` | `stdio` | Transport: `stdio` or `sse` |
| `MCAP_SSE_PORT` | `8080` | Port for SSE transport |
| `MCAP_FLATTEN_DEPTH` | `3` | Max nesting depth for message flattening |
| `FOXGLOVE_API_KEY` | — | Foxglove Data Platform API key (also accepted as `MCAP_FOXGLOVE_API_KEY`) |
| `MCAP_FOXGLOVE_API_URL` | `https://api.foxglove.dev` | Foxglove API base URL |
| `MCAP_FOXGLOVE_DOWNLOAD_DIR` | `<data_dir>/foxglove` | Where imported recordings are written |
| `MCAP_FOXGLOVE_IMPORT_TIMEOUT_S` | `900` | How long to wait for a device to upload a recording |

## TOML config file

Optional. Place a `mcap-mcp-server.toml` in the working directory:

```toml
[server]
data_dir = "/data/recordings"
recursive = true
transport = "stdio"

[limits]
max_memory_mb = 4096
query_timeout_s = 60
default_row_limit = 1000
max_row_limit = 50000

[decoder]
flatten_depth = 3

[logging]
level = "INFO"

[foxglove]
api_key = "fox_sk_..."
api_url = "https://api.foxglove.dev"
download_dir = "/data/recordings/foxglove"
import_timeout_s = 900
```

## Foxglove import

`list_foxglove_recordings` and `import_foxglove_recording` need an API key created under
**Settings → API keys** in Foxglove. The key needs read access to recordings and permission to
import them. Prefer the environment variable over the TOML file so the key stays out of version
control:

```json
{
  "mcpServers": {
    "mcap-query": {
      "command": "uvx",
      "args": ["mcap-mcp-server[all]"],
      "env": { "FOXGLOVE_API_KEY": "fox_sk_..." }
    }
  }
}
```

Without a key the two Foxglove tools return an `error` explaining what to set; every other tool
keeps working on local files.

## MCP client integration

### Cursor (`.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "mcap-query": {
      "command": "uvx",
      "args": ["mcap-mcp-server[all]"]
    }
  }
}
```

> Set `MCAP_DATA_DIR` only if recordings live outside the project directory.

### Claude Desktop (`claude_desktop_config.json`)

Same format as Cursor.

### Docker (SSE)

```bash
docker run -d \
  -v /data/recordings:/data:ro \
  -e MCAP_DATA_DIR=/data \
  -e MCAP_TRANSPORT=sse \
  -p 8080:8080 \
  ghcr.io/turkenberg/mcap-mcp-server:latest
```

Then in Cursor: `"url": "http://localhost:8080/sse"`.
