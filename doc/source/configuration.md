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
| `MCAP_TRANSPORT` | `stdio` | Transport: `stdio`, `http` (remote server), or `sse` (deprecated) |
| `MCAP_SSE_PORT` | `8080` | Port for the http/sse transport |
| `MCAP_BASE_URL` | — | Public URL of the server (required for Google auth) |
| `MCAP_GOOGLE_CLIENT_ID` | — | Google OAuth client ID — setting it enables Google Workspace login |
| `MCAP_GOOGLE_CLIENT_SECRET` | — | Google OAuth client secret |
| `MCAP_ALLOWED_GOOGLE_DOMAINS` | — | Comma-separated Workspace domains allowed to log in, e.g. `lvairo.com` |
| `MCAP_INSECURE_NO_AUTH` | `false` | Explicitly allow serving over HTTP without authentication |
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

[auth]
google_client_id = "1234567890.apps.googleusercontent.com"
google_client_secret = "GOCSPX-..."
allowed_domains = ["lvairo.com"]
```

(For a remote server also set `base_url` in the `[server]` section.)

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

## Remote server with Google Workspace login

The server can be shared with a whole team over HTTP. It then acts as a standard OAuth-protected MCP server: clients (Claude, Cursor, …) discover the auth endpoints automatically, open a browser window for the Google login, and only accounts on the configured Workspace domains are let in — the domain is verified server-side on every request, so a random Google account cannot get a session even if it completes the login flow.

### 1. Create a Google OAuth client

In the [Google Cloud Console](https://console.cloud.google.com/apis/credentials) (any project of your Workspace):

1. **APIs & Services → Credentials → Create credentials → OAuth client ID**, type **Web application**.
2. Add the authorized redirect URI `https://<your-server>/auth/callback`.
3. If asked to configure the consent screen, set the user type to **Internal** — that alone already limits logins to your Workspace, and the server enforces the domain again on top.
4. Note the client ID and client secret.

### 2. Run the server

```bash
docker run -d \
  -v /data/recordings:/data \
  -e MCAP_TRANSPORT=http \
  -e MCAP_BASE_URL=https://mcap.example.com \
  -e MCAP_GOOGLE_CLIENT_ID=1234567890.apps.googleusercontent.com \
  -e MCAP_GOOGLE_CLIENT_SECRET=GOCSPX-... \
  -e MCAP_ALLOWED_GOOGLE_DOMAINS=lvairo.com \
  -e FOXGLOVE_API_KEY=fox_sk_... \
  -p 8080:8080 \
  ghcr.io/turkenberg/mcap-mcp-server:latest
```

Put the container behind TLS (a reverse proxy such as Caddy or Traefik, or your ingress) — `MCAP_BASE_URL` must be the public HTTPS URL. Note that the data volume is **not** mounted read-only here: `load_interval` and `import_foxglove_recording` write downloaded recordings into `<data_dir>/foxglove`. The Foxglove API key stays on the server, so team members never need one of their own.

With `MCAP_TRANSPORT=http` and no Google client configured, the server refuses to start rather than serving your recordings to anyone who can reach the port. Set `MCAP_INSECURE_NO_AUTH=true` only for local experiments.

### 3. Connect clients

Team members add the server by URL — no keys, no local install:

```json
{
  "mcpServers": {
    "mcap-query": { "url": "https://mcap.example.com/mcp" }
  }
}
```

or `claude mcp add --transport http mcap-query https://mcap.example.com/mcp`. The first use opens the Google login in a browser.

One caveat of the shared deployment: all users share one DuckDB memory budget, so a `load_interval` by one user can evict tables another user loaded (the eviction is reported in tool responses).
