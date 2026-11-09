"""A viz instance's project database (the "data centre").

When a ``DataVisualisation`` carries ``spec.imDataManager.dataCentre`` the
operator hands the Pod the project's database credentials as
``DATA_CENTRE_PG_*`` variables. The block's optional ``maxBytes`` (the
project's size limit) becomes ``DATA_CENTRE_MAX_BYTES``; the viz app applies
the limit. Without the block the Pod is launched as before
(its Data API then answers 503 and the rest of the app works).

The operator does not provision anything. The Account Server (AS), as the
superuser only it holds, creates the database, the RDKit cartridge and the
project's role (with its limits) and schema. The Data Manager passes on only
that project's credentials, which arrive here in the block.

Tested in ``tests/test_datacentre.py``.
"""

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import kopf

_DEFAULT_PORT: int = 5432


@dataclass(frozen=True)
class DataCentre:
    """The CR's ``dataCentre`` block, validated and with defaults applied."""

    host: str
    port: int
    ssl: bool
    database: str
    schema: str
    role: str
    password: str = field(repr=False)
    max_bytes: Optional[int] = None


def _required_string(block: Dict[str, Any], key: str) -> str:
    value = block.get(key)
    if not isinstance(value, str) or not value:
        raise kopf.PermanentError(f"dataCentre.{key} must be a non-empty string")
    return value


def _is_positive_int(value: Any) -> bool:
    # bool is an int in Python; it is never what was meant here.
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _port(block: Dict[str, Any]) -> int:
    value = block.get("port", _DEFAULT_PORT)
    if _is_positive_int(value):
        return int(value)
    raise kopf.PermanentError("dataCentre.port must be a positive integer")


def _max_bytes(block: Dict[str, Any]) -> Optional[int]:
    value = block.get("maxBytes")
    if value is None or _is_positive_int(value):
        return value
    raise kopf.PermanentError("dataCentre.maxBytes must be a positive integer")


def parse_data_centre(material: Dict[str, Any]) -> Optional[DataCentre]:
    """Read ``imDataManager.dataCentre``; ``None`` when it is absent.

    A malformed block is a ``kopf.PermanentError``: retrying cannot fix it.
    """
    block = material.get("dataCentre")
    if block is None:
        return None
    if not isinstance(block, dict):
        raise kopf.PermanentError("dataCentre must be an object")
    ssl = block.get("ssl", False)
    if not isinstance(ssl, bool):
        raise kopf.PermanentError("dataCentre.ssl must be a boolean")
    return DataCentre(
        host=_required_string(block, "host"),
        port=_port(block),
        ssl=ssl,
        database=_required_string(block, "database"),
        schema=_required_string(block, "schema"),
        role=_required_string(block, "role"),
        password=_required_string(block, "password"),
        max_bytes=_max_bytes(block),
    )


def build_data_centre_environment(dc: DataCentre) -> List[Dict[str, str]]:
    """The ``DATA_CENTRE_*`` variables the viz app reads."""
    env: List[Dict[str, str]] = [
        {"name": "DATA_CENTRE_PG_HOST", "value": dc.host},
        {"name": "DATA_CENTRE_PG_PORT", "value": str(dc.port)},
        {"name": "DATA_CENTRE_PG_DATABASE", "value": dc.database},
        {"name": "DATA_CENTRE_PG_SCHEMA", "value": dc.schema},
        {"name": "DATA_CENTRE_PG_ROLE", "value": dc.role},
        {"name": "DATA_CENTRE_PG_SECRET", "value": dc.password},
    ]
    if dc.ssl:
        env.append({"name": "DATA_CENTRE_PG_SSL", "value": "true"})
    if dc.max_bytes is not None:
        env.append({"name": "DATA_CENTRE_MAX_BYTES", "value": str(dc.max_bytes)})
    return env


def redact_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    """A copy of the CR spec that is safe to log (the password masked)."""
    block = spec.get("imDataManager", {}).get("dataCentre")
    if not isinstance(block, dict) or "password" not in block:
        return spec
    redacted = copy.deepcopy(spec)
    redacted["imDataManager"]["dataCentre"]["password"] = "***"
    return redacted
