FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src/ src/

RUN pip install --no-cache-dir ".[all]"

ENV MCAP_DATA_DIR=/data
ENV MCAP_TRANSPORT=http

# Remote use requires Google Workspace auth. Provide at runtime:
#   MCAP_BASE_URL, MCAP_GOOGLE_CLIENT_ID, MCAP_GOOGLE_CLIENT_SECRET,
#   MCAP_ALLOWED_GOOGLE_DOMAINS (e.g. "lvairo.com"), FOXGLOVE_API_KEY

EXPOSE 8080

ENTRYPOINT ["mcap-mcp-server"]
