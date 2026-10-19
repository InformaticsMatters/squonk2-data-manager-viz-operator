"""Unit tests for the data-centre (project database) logic.

These cover everything in ``operator/datacentre.py``: reading the CR's
``dataCentre`` block, building the Pod environment and redacting the spec
before it is logged.
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
        "schema": f"p_{PROJECT_KEY}",
        "role": f"u_{PROJECT_KEY}",
        "password": "s3cret",
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
    assert dc.schema == f"p_{PROJECT_KEY}"
    assert dc.role == f"u_{PROJECT_KEY}"
    assert dc.password == "s3cret"


def test_explicit_values_are_read() -> None:
    dc = datacentre.parse_data_centre(_material(port=6543, ssl=True))
    assert dc is not None
    assert (dc.port, dc.ssl) == (6543, True)


@pytest.mark.parametrize("missing", ["host", "database", "schema", "role", "password"])
def test_missing_required_field_is_permanent_error(missing: str) -> None:
    material = _material()
    del material["dataCentre"][missing]
    with pytest.raises(kopf.PermanentError, match=missing):
        datacentre.parse_data_centre(material)


@pytest.mark.parametrize(
    "field, value",
    [
        ("port", 0),
        ("port", "5432"),
        ("port", True),
        ("ssl", "true"),
        ("role", ""),
        ("password", 42),
    ],
)
def test_invalid_values_are_permanent_errors(field: str, value: Any) -> None:
    with pytest.raises(kopf.PermanentError, match=field):
        datacentre.parse_data_centre(_material(**{field: value}))


def test_block_must_be_an_object() -> None:
    with pytest.raises(kopf.PermanentError):
        datacentre.parse_data_centre({"dataCentre": "pg.example"})


def test_password_is_not_in_the_repr() -> None:
    dc = datacentre.parse_data_centre(_material())
    assert "s3cret" not in repr(dc)


# --- build_data_centre_environment ---------------------------------------------


def _env(**data_centre: Any) -> Dict[str, str]:
    dc = datacentre.parse_data_centre(_material(**data_centre))
    assert dc is not None
    env = datacentre.build_data_centre_environment(dc)
    names = [e["name"] for e in env]
    assert len(names) == len(set(names))
    return {e["name"]: e["value"] for e in env}


def test_environment_carries_everything_the_app_needs() -> None:
    assert _env() == {
        "DATA_CENTRE_PG_HOST": "pg.example",
        "DATA_CENTRE_PG_PORT": "5432",
        "DATA_CENTRE_PG_DATABASE": "org_abc",
        "DATA_CENTRE_PG_SCHEMA": f"p_{PROJECT_KEY}",
        "DATA_CENTRE_PG_ROLE": f"u_{PROJECT_KEY}",
        "DATA_CENTRE_PG_SECRET": "s3cret",
    }


def test_environment_sets_ssl_only_when_asked() -> None:
    assert _env(ssl=True)["DATA_CENTRE_PG_SSL"] == "true"
    assert "DATA_CENTRE_PG_SSL" not in _env(ssl=False)


# --- redact_spec ---------------------------------------------------------------


def test_redact_spec_hides_the_password_only() -> None:
    spec = {"imDataManager": _material()}
    redacted = datacentre.redact_spec(spec)
    assert "s3cret" not in repr(redacted)
    assert redacted["imDataManager"]["dataCentre"]["password"] == "***"
    assert redacted["imDataManager"]["dataCentre"]["host"] == "pg.example"
    # The original is untouched - the handler still needs the real value.
    assert spec["imDataManager"]["dataCentre"]["password"] == "s3cret"


def test_redact_spec_leaves_a_spec_without_the_block_alone() -> None:
    spec = {"imDataManager": {"project": {"id": PROJECT_ID}}}
    assert datacentre.redact_spec(spec) == spec
