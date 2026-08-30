"""Configuration loading from environment variables and optional TOML file."""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

_TOML_FILENAME = "mcap-mcp-server.toml"


@dataclass
class ServerConfig:
    data_dir: Path = field(default_factory=lambda: Path("."))
    recursive: bool = True
    max_memory_mb: int = 2048
    query_timeout_s: int = 30
    default_row_limit: int = 1000
    max_row_limit: int = 10000
    log_level: str = "INFO"
    transport: str = "stdio"
    sse_port: int = 8080
    flatten_depth: int = 3
    foxglove_api_key: str = ""
    foxglove_api_url: str = "https://api.foxglove.dev"
    foxglove_download_dir: Path | None = None
    foxglove_import_timeout_s: int = 900

    def __post_init__(self) -> None:
        if self.max_memory_mb < 64:
            raise ValueError(
                f"MCAP_MAX_MEMORY_MB={self.max_memory_mb} is too low. "
                "At least 64 MB is required to load and query recordings."
            )

    @property
    def foxglove_dir(self) -> Path:
        """Directory that imported Foxglove recordings are written to."""
        if self.foxglove_download_dir is not None:
            return self.foxglove_download_dir
        return self.data_dir / "foxglove"

    def configure_logging(self) -> None:
        logging.basicConfig(
            level=getattr(logging, self.log_level.upper(), logging.INFO),
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )


def _load_toml(path: Path) -> dict:
    """Load a TOML config file, returning empty dict if not found."""
    if not path.is_file():
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception:
        logger.warning("Failed to parse config file %s, ignoring", path)
        return {}


def _optional_path(value: str | None) -> Path | None:
    return Path(value) if value else None


def _bool_env(value: str) -> bool:
    return value.lower() in ("1", "true", "yes")


def load_config(
    toml_path: Path | None = None,
    cli_overrides: dict | None = None,
) -> ServerConfig:
    """Build config by layering: defaults < TOML < env vars < CLI overrides."""
    toml_data: dict = {}
    if toml_path is None:
        toml_path = Path(_TOML_FILENAME)
    toml_data = _load_toml(toml_path)

    server_section = toml_data.get("server", {})
    limits_section = toml_data.get("limits", {})
    decoder_section = toml_data.get("decoder", {})
    logging_section = toml_data.get("logging", {})
    foxglove_section = toml_data.get("foxglove", {})

    cfg = ServerConfig(
        data_dir=Path(server_section.get("data_dir", ".")),
        recursive=server_section.get("recursive", True),
        transport=server_section.get("transport", "stdio"),
        max_memory_mb=limits_section.get("max_memory_mb", 2048),
        query_timeout_s=limits_section.get("query_timeout_s", 30),
        default_row_limit=limits_section.get("default_row_limit", 1000),
        max_row_limit=limits_section.get("max_row_limit", 10000),
        flatten_depth=decoder_section.get("flatten_depth", 3),
        log_level=logging_section.get("level", "INFO"),
        foxglove_api_key=foxglove_section.get("api_key", ""),
        foxglove_api_url=foxglove_section.get("api_url", "https://api.foxglove.dev"),
        foxglove_download_dir=_optional_path(foxglove_section.get("download_dir")),
        foxglove_import_timeout_s=foxglove_section.get("import_timeout_s", 900),
    )

    env_map = {
        "MCAP_DATA_DIR": ("data_dir", Path),
        "MCAP_RECURSIVE": ("recursive", _bool_env),
        "MCAP_MAX_MEMORY_MB": ("max_memory_mb", int),
        "MCAP_QUERY_TIMEOUT_S": ("query_timeout_s", int),
        "MCAP_DEFAULT_ROW_LIMIT": ("default_row_limit", int),
        "MCAP_MAX_ROW_LIMIT": ("max_row_limit", int),
        "MCAP_LOG_LEVEL": ("log_level", str),
        "MCAP_TRANSPORT": ("transport", str),
        "MCAP_SSE_PORT": ("sse_port", int),
        "MCAP_FLATTEN_DEPTH": ("flatten_depth", int),
        "MCAP_FOXGLOVE_API_KEY": ("foxglove_api_key", str),
        "FOXGLOVE_API_KEY": ("foxglove_api_key", str),
        "MCAP_FOXGLOVE_API_URL": ("foxglove_api_url", str),
        "MCAP_FOXGLOVE_DOWNLOAD_DIR": ("foxglove_download_dir", Path),
        "MCAP_FOXGLOVE_IMPORT_TIMEOUT_S": ("foxglove_import_timeout_s", int),
    }
    for env_key, (attr, converter) in env_map.items():
        val = os.environ.get(env_key)
        if val is not None:
            try:
                setattr(cfg, attr, converter(val))
            except (ValueError, TypeError):
                logger.warning("Invalid value for %s=%r, ignoring", env_key, val)

    if cli_overrides:
        for key, value in cli_overrides.items():
            if value is not None and hasattr(cfg, key):
                setattr(cfg, key, value)

    return cfg
