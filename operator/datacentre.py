"""Provisioning a viz instance's project database (the "data centre").

When a ``DataVisualisation`` carries ``spec.imDataManager.dataCentre`` the
operator makes sure its project has a role and a schema in the database named
there, and hands the Pod the details as ``DATA_CENTRE_PG_*`` variables. Without
the block nothing here runs and the Pod is launched as before (its Data API then
answers 503 and the rest of the app works).

The database itself is created elsewhere (by the Account Server); everything
inside it that a project needs is ensured here, idempotently, on every launch:

- the RDKit cartridge (``CREATE EXTENSION rdkit``), which needs a superuser;
- ``CONNECT`` revoked from ``PUBLIC``, so only roles granted it may connect;
- role ``u_<key>``, with its limits, ``CONNECT`` on this database only and its
  ``search_path`` set to its schema;
- schema ``p_<key>``, owned by that role.

``<key>`` is the project id reduced to lowercase hex, matching the reference
provisioner in squonk2-viz-components (``apps/data-centre/src/provisioner``).
The role's password is derived from the admin password and the key rather than
stored, so every instance of a project computes the same one and nothing needs
to be kept anywhere. The tables are the viz app's business.

The pure parts are tested in ``tests/test_datacentre.py``, the SQL against a
real Postgres in ``tests/test_datacentre_pg.py``.
"""

import base64
import copy
import hashlib
import hmac
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import kopf
import psycopg
from psycopg import sql

_DEFAULT_PORT: int = 5432
# Defaults match the reference provisioner's.
_DEFAULT_STATEMENT_TIMEOUT_S: int = 60
_DEFAULT_TEMP_FILE_LIMIT_MB: int = 2048
_DEFAULT_CONNECTION_LIMIT: int = 8
# How long to wait for the database server before giving up (and retrying).
_CONNECT_TIMEOUT_S: int = 10

# Launches are rare and the work is quick, so every launch against a server is
# serialised on one advisory lock. That covers two launches of one project
# racing on CREATE ROLE, and two first launches racing on CREATE EXTENSION.
_LOCK_NAME: str = "squonk2-viz-operator:data-centre"

# A UUID with or without hyphens, or shorter hex - optionally prefixed
# 'project-', as the Data Manager names projects.
_PROJECT_ID_RE = re.compile(r"^(?:project-)?([0-9a-fA-F]{8}(?:-?[0-9a-fA-F]){0,24})$")


@dataclass(frozen=True)
class DataCentre:
    """The CR's ``dataCentre`` block, validated and with defaults applied."""

    host: str
    port: int
    ssl: bool
    database: str
    admin_user: str
    admin_password: str = field(repr=False)
    statement_timeout_s: int
    temp_file_limit_mb: int
    connection_limit: int


@dataclass(frozen=True)
class ProjectDatabase:
    """What a project is given: its schema, its role and the role's password."""

    schema: str
    role: str
    secret: str = field(repr=False)


def _required_string(block: Dict[str, Any], key: str) -> str:
    value = block.get(key)
    if not isinstance(value, str) or not value:
        raise kopf.PermanentError(f"dataCentre.{key} must be a non-empty string")
    return value


def _integer(block: Dict[str, Any], key: str, default: int, minimum: int) -> int:
    value = block.get(key, default)
    # bool is an int in Python; it is never what was meant here.
    if isinstance(value, int) and not isinstance(value, bool) and value >= minimum:
        return value
    raise kopf.PermanentError(
        f"dataCentre {key} must be an integer of at least {minimum}"
    )


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
    limits = block.get("limits", {})
    if not isinstance(limits, dict):
        raise kopf.PermanentError("dataCentre.limits must be an object")
    return DataCentre(
        host=_required_string(block, "host"),
        port=_integer(block, "port", _DEFAULT_PORT, 1),
        ssl=ssl,
        database=_required_string(block, "database"),
        admin_user=_required_string(block, "adminUser"),
        admin_password=_required_string(block, "adminPassword"),
        statement_timeout_s=_integer(
            limits, "statementTimeout", _DEFAULT_STATEMENT_TIMEOUT_S, 1
        ),
        temp_file_limit_mb=_integer(
            limits, "tempFileLimitMB", _DEFAULT_TEMP_FILE_LIMIT_MB, 0
        ),
        connection_limit=_integer(
            limits, "connectionLimit", _DEFAULT_CONNECTION_LIMIT, 1
        ),
    )


def project_key(project_id: Any) -> str:
    """The project id as the lowercase hex its role and schema are named by.

    Anything other than a (``project-``-prefixed) UUID or hex string is refused
    before it can name anything.
    """
    match = _PROJECT_ID_RE.match(project_id) if isinstance(project_id, str) else None
    key = match.group(1).replace("-", "").lower() if match else ""
    if not key or len(key) > 32:
        raise kopf.PermanentError(
            f"project id must be a UUID or hex string, got {str(project_id)[:48]!r}"
        )
    return key


def project_database(dc: DataCentre, project_id: Any) -> ProjectDatabase:
    """Name a project's schema and role, and derive the role's password.

    The password is ``base64url(HMAC-SHA256(adminPassword, key))``: the same for
    every instance of the project, so a second launch never invalidates the
    first, and nothing has to be stored to remember it.
    """
    key = project_key(project_id)
    digest = hmac.new(dc.admin_password.encode(), key.encode(), hashlib.sha256).digest()
    secret = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return ProjectDatabase(schema=f"p_{key}", role=f"u_{key}", secret=secret)


def build_data_centre_environment(
    dc: DataCentre, pdb: ProjectDatabase
) -> List[Dict[str, str]]:
    """The ``DATA_CENTRE_PG_*`` variables the viz app reads (and nothing of the admin's)."""
    env: List[Dict[str, str]] = [
        {"name": "DATA_CENTRE_PG_HOST", "value": dc.host},
        {"name": "DATA_CENTRE_PG_PORT", "value": str(dc.port)},
        {"name": "DATA_CENTRE_PG_DATABASE", "value": dc.database},
        {"name": "DATA_CENTRE_PG_SCHEMA", "value": pdb.schema},
        {"name": "DATA_CENTRE_PG_ROLE", "value": pdb.role},
        {"name": "DATA_CENTRE_PG_SECRET", "value": pdb.secret},
    ]
    if dc.ssl:
        env.append({"name": "DATA_CENTRE_PG_SSL", "value": "true"})
    return env


def redact_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    """A copy of the CR spec that is safe to log (the admin password masked)."""
    block = spec.get("imDataManager", {}).get("dataCentre")
    if not isinstance(block, dict) or "adminPassword" not in block:
        return spec
    redacted = copy.deepcopy(spec)
    redacted["imDataManager"]["dataCentre"]["adminPassword"] = "***"
    return redacted


def ensure_project_database(dc: DataCentre, pdb: ProjectDatabase) -> None:
    """Make the project's role and schema exist, exactly as described above.

    Safe to repeat. Any failure is a ``kopf.TemporaryError`` (the database may
    not have been created yet, or the server may be briefly unreachable) whose
    message never carries the admin password.
    """
    try:
        with psycopg.connect(
            host=dc.host,
            port=dc.port,
            dbname=dc.database,
            user=dc.admin_user,
            password=dc.admin_password,
            sslmode="require" if dc.ssl else "prefer",
            connect_timeout=_CONNECT_TIMEOUT_S,
            autocommit=True,
        ) as conn:
            conn.execute("SELECT pg_advisory_lock(hashtext(%s))", (_LOCK_NAME,))
            try:
                _ensure(conn, dc, pdb)
            finally:
                conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (_LOCK_NAME,))
    except psycopg.Error as exc:
        message = str(exc).replace(dc.admin_password, "***").strip()
        raise kopf.TemporaryError(
            f"Could not provision {pdb.schema} in database {dc.database!r}: {message}"
        ) from None


def _ensure(
    conn: psycopg.Connection[Any], dc: DataCentre, pdb: ProjectDatabase
) -> None:
    database = sql.Identifier(dc.database)
    role = sql.Identifier(pdb.role)
    schema = sql.Identifier(pdb.schema)
    password = sql.Literal(pdb.secret)
    limit = sql.Literal(dc.connection_limit)

    conn.execute("CREATE EXTENSION IF NOT EXISTS rdkit")
    conn.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(database))

    exists = conn.execute(
        "SELECT 1 FROM pg_roles WHERE rolname = %s", (pdb.role,)
    ).fetchone()
    verb = "ALTER" if exists else "CREATE"
    # Setting the password on every launch also restores one changed by hand.
    conn.execute(
        sql.SQL(verb + " ROLE {} LOGIN PASSWORD {} CONNECTION LIMIT {}").format(
            role, password, limit
        )
    )
    conn.execute(
        sql.SQL("ALTER ROLE {} SET statement_timeout = {}").format(
            role, sql.Literal(dc.statement_timeout_s * 1000)
        )
    )
    conn.execute(
        sql.SQL("ALTER ROLE {} SET temp_file_limit = {}").format(
            role, sql.Literal(dc.temp_file_limit_mb * 1024)
        )
    )
    conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(database, role))
    conn.execute(
        sql.SQL("ALTER ROLE {} IN DATABASE {} SET search_path = {}, public").format(
            role, database, schema
        )
    )
    conn.execute(
        sql.SQL("CREATE SCHEMA IF NOT EXISTS {} AUTHORIZATION {}").format(schema, role)
    )
