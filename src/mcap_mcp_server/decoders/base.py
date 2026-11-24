"""Base protocol and data structures for message decoders."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


def schema_cache_key(schema_name: str, schema_data: bytes) -> tuple[str, str]:
    """Cache key for a parsed schema that is stable across files.

    MCAP schema ids are only unique within a single file — the same id means
    a different schema in the next file. A long-lived server loads many files,
    so schema-derived state must be cached by content, never by id.
    """
    return schema_name, hashlib.sha1(schema_data).hexdigest()


@dataclass(frozen=True)
class FieldInfo:
    """Describes a single field extracted from a message schema."""

    name: str
    type: str  # DuckDB type name (VARCHAR, DOUBLE, BIGINT, etc.)
    description: str = ""


# JSON Schema type -> DuckDB type mapping
JSON_SCHEMA_TYPE_MAP: dict[str, str] = {
    "boolean": "BOOLEAN",
    "integer": "BIGINT",
    "number": "DOUBLE",
    "string": "VARCHAR",
    "array": "VARCHAR",  # serialised as JSON in v1
    "object": "VARCHAR",  # serialised as JSON if beyond flatten depth
}

# Protobuf / ROS type -> DuckDB type mapping
NUMERIC_TYPE_MAP: dict[str, str] = {
    "bool": "BOOLEAN",
    "int8": "TINYINT",
    "uint8": "UTINYINT",
    "byte": "UTINYINT",
    "int16": "SMALLINT",
    "uint16": "USMALLINT",
    "int32": "INTEGER",
    "int": "INTEGER",
    "uint32": "UINTEGER",
    "int64": "BIGINT",
    "long": "BIGINT",
    "uint64": "UBIGINT",
    "float32": "FLOAT",
    "float": "FLOAT",
    "float64": "DOUBLE",
    "double": "DOUBLE",
    "string": "VARCHAR",
    "bytes": "BLOB",
    "char": "UTINYINT",
    "time": "BIGINT",
    "duration": "BIGINT",
}


@runtime_checkable
class MessageDecoder(Protocol):
    """Interface for MCAP message decoders.

    Implementations handle specific encoding types (JSON, Protobuf, etc.)
    and convert raw bytes into flat Python dicts suitable for DataFrame
    construction.
    """

    def can_decode(self, message_encoding: str, schema_encoding: str) -> bool:
        """Return True if this decoder handles the given encoding pair."""
        ...

    def decode(
        self,
        schema: bytes,
        data: bytes,
        *,
        schema_name: str = "",
        schema_encoding: str = "",
        schema_id: int = 0,
    ) -> dict[str, Any]:
        """Decode a single message into a flat dict of field -> value.

        Factory-based decoders use the extra kwargs to construct the
        ``mcap.records.Schema`` that the official DecoderFactory expects.
        """
        ...

    def get_field_info(self, schema: bytes, schema_encoding: str) -> list[FieldInfo]:
        """Extract field names and DuckDB types from the schema definition."""
        ...
