"""Unit tests for the data-centre (project database) logic.

These cover everything in ``operator/datacentre.py`` that does not need a
database: reading the CR's ``dataCentre`` block, naming a project's role and
schema, deriving its password, building the Pod environment and redacting the
spec before it is logged. The SQL itself is exercised against a real Postgres
in ``test_datacentre_pg.py``.
"""

import os
import sys
from typing import Any, Dict

import kopf
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "operator"))

import datacentre  # noqa: E402  pylint: disable=wrong-import-position

PROJECT_ID = "project-7f3c9a2e-1b4d-4c8e-9f0a-2d6e8b1c3a5f"
PROJECT_KEY = "7f3c9a2e1b4d4c8e9f0a2d6e8b1c3a5f"


def _material(**data_centre: Any) -> Dict[str, Any]:
    block: Dict[str, Any] = {
        "host": "pg.example",
        "database": "org_abc",
        "adminUser": "postgres",
        "adminPassword": "s3cret",
    }
    block.update(data_centre)
    return {"project": {"id": PROJECT_ID}, "dataCentre": block}


# --- parse_data_centre -------------------------------------------------------


def test_absent_block_means_feature_off() -> None:
    assert datacentre.parse_data_centre({"project": {"id": PROJECT_ID}}) is None


def test_defaults_are_applied() -> None:
    dc = datacentre.parse_data_centre(_material())
    assert dc is not None
    assert dc.host == "pg.example"
    assert dc.port == 5432
    assert dc.ssl is False
    assert dc.database == "org_abc"
    assert dc.admin_user == "postgres"
    assert dc.admin_password == "s3cret"
    assert dc.statement_timeout_s == 60
    assert dc.temp_file_limit_mb == 2048
    assert dc.connection_limit == 8


def test_explicit_values_and_limits_are_read() -> None:
    dc = datacentre.parse_data_centre(
        _material(
            port=6543,
            ssl=True,
            limits={
                "statementTimeout": 30,
                "tempFileLimitMB": 512,
                "connectionLimit": 4,
            },
        )
    )
    assert dc is not None
    assert (dc.port, dc.ssl) == (6543, True)
    assert (dc.statement_timeout_s, dc.temp_file_limit_mb, dc.connection_limit) == (
        30,
        512,
        4,
    )


@pytest.mark.parametrize("missing", ["host", "database", "adminUser", "adminPassword"])
def test_missing_required_field_is_permanent_error(missing: str) -> None:
    material = _material()
    del material["dataCentre"][missing]
    with pytest.raises(kopf.PermanentError, match=missing):
        datacentre.parse_data_centre(material)


@pytest.mark.parametrize(
    "limits",
    [
        {"statementTimeout": 0},
        {"tempFileLimitMB": -1},
        {"connectionLimit": 0},
        {"connectionLimit": "8"},
    ],
)
def test_invalid_limits_are_permanent_errors(limits: Dict[str, Any]) -> None:
    with pytest.raises(kopf.PermanentError):
        datacentre.parse_data_centre(_material(limits=limits))


def test_password_is_not_in_the_repr() -> None:
    dc = datacentre.parse_data_centre(_material())
    assert "s3cret" not in repr(dc)


# --- project_key -------------------------------------------------------------


@pytest.mark.parametrize(
    "project_id",
    [
        PROJECT_ID,
        "7f3c9a2e-1b4d-4c8e-9f0a-2d6e8b1c3a5f",
        "7F3C9A2E1B4D4C8E9F0A2D6E8B1C3A5F",
    ],
)
def test_project_key_reduces_a_dm_project_id_to_lowercase_hex(project_id: str) -> None:
    assert datacentre.project_key(project_id) == PROJECT_KEY


@pytest.mark.parametrize(
    "project_id",
    [
        None,
        "",
        "project-",
        "project-000",  # too short to be an id
        "project-7f3c9a2e; DROP ROLE x",
        'project-7f3c9a2e"',
        "instance-7f3c9a2e-1b4d-4c8e-9f0a-2d6e8b1c3a5f",
        "7f3c9a2e1b4d4c8e9f0a2d6e8b1c3a5f00",  # longer than a UUID
    ],
)
def test_project_key_refuses_anything_else(project_id: Any) -> None:
    with pytest.raises(kopf.PermanentError):
        datacentre.project_key(project_id)


# --- project_database ---------------------------------------------------------


def test_project_names_follow_the_provisioner() -> None:
    dc = datacentre.parse_data_centre(_material())
    assert dc is not None
    pdb = datacentre.project_database(dc, PROJECT_ID)
    assert pdb.schema == f"p_{PROJECT_KEY}"
    assert pdb.role == f"u_{PROJECT_KEY}"


def test_secret_is_stable_for_a_project() -> None:
    dc = datacentre.parse_data_centre(_material())
    assert dc is not None
    first = datacentre.project_database(dc, PROJECT_ID).secret
    again = datacentre.project_database(
        dc, PROJECT_ID.upper().replace("PROJECT-", "project-")
    ).secret
    assert first == again
    # 32 bytes of HMAC, base64url without padding.
    assert len(first) == 43
    assert set(first) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    )


def test_secret_differs_between_projects_and_admin_passwords() -> None:
    dc = datacentre.parse_data_centre(_material())
    other_admin = datacentre.parse_data_centre(_material(adminPassword="different"))
    assert dc is not None and other_admin is not None
    a = datacentre.project_database(dc, PROJECT_ID).secret
    b = datacentre.project_database(
        dc, "project-00000000-0000-0000-0000-000000000001"
    ).secret
    c = datacentre.project_database(other_admin, PROJECT_ID).secret
    assert len({a, b, c}) == 3


def test_secret_is_not_in_the_repr() -> None:
    dc = datacentre.parse_data_centre(_material())
    assert dc is not None
    pdb = datacentre.project_database(dc, PROJECT_ID)
    assert pdb.secret not in repr(pdb)


# --- build_data_centre_environment ---------------------------------------------


def _env(**data_centre: Any) -> Dict[str, str]:
    dc = datacentre.parse_data_centre(_material(**data_centre))
    assert dc is not None
    pdb = datacentre.project_database(dc, PROJECT_ID)
    env = datacentre.build_data_centre_environment(dc, pdb)
    names = [e["name"] for e in env]
    assert len(names) == len(set(names))
    return {e["name"]: e["value"] for e in env}


def test_environment_carries_everything_the_app_needs() -> None:
    env = _env()
    assert env == {
        "DATA_CENTRE_PG_HOST": "pg.example",
        "DATA_CENTRE_PG_PORT": "5432",
        "DATA_CENTRE_PG_DATABASE": "org_abc",
        "DATA_CENTRE_PG_SCHEMA": f"p_{PROJECT_KEY}",
        "DATA_CENTRE_PG_ROLE": f"u_{PROJECT_KEY}",
        "DATA_CENTRE_PG_SECRET": env["DATA_CENTRE_PG_SECRET"],
    }
    assert len(env["DATA_CENTRE_PG_SECRET"]) == 43


def test_environment_sets_ssl_only_when_asked() -> None:
    assert _env(ssl=True)["DATA_CENTRE_PG_SSL"] == "true"
    assert "DATA_CENTRE_PG_SSL" not in _env(ssl=False)


def test_environment_never_carries_the_admin_credential() -> None:
    values = _env().values()
    assert "s3cret" not in values
    assert "postgres" not in values


# --- redact_spec ---------------------------------------------------------------


def test_redact_spec_hides_the_admin_password_only() -> None:
    spec = {"imDataManager": _material()}
    redacted = datacentre.redact_spec(spec)
    assert "s3cret" not in repr(redacted)
    assert redacted["imDataManager"]["dataCentre"]["adminPassword"] == "***"
    assert redacted["imDataManager"]["dataCentre"]["host"] == "pg.example"
    # The original is untouched - the handler still needs the real value.
    assert spec["imDataManager"]["dataCentre"]["adminPassword"] == "s3cret"


def test_redact_spec_leaves_a_spec_without_the_block_alone() -> None:
    spec = {"imDataManager": {"project": {"id": PROJECT_ID}}}
    assert datacentre.redact_spec(spec) == spec
