"""Integration tests for project-database provisioning, against real Postgres.

They need a Postgres with the RDKit cartridge and a superuser, named by
``SVO_TEST_PG_URL``, e.g.::

    docker run -d --rm --name svo-dc-test -e POSTGRES_PASSWORD=svo-test \\
        -p 127.0.0.1:55432:5432 \\
        informaticsmatters/rdkit-cartridge-debian:Release_2024_03_3
    SVO_TEST_PG_URL=postgresql://postgres:svo-test@127.0.0.1:55432/postgres pytest

Without it they are skipped. Each run creates its own databases (standing in
for the ones the Account Server creates) and removes them, and the roles it
made, afterwards.
"""

import os
import sys
import threading
import uuid
from typing import Any, Dict, Iterator, List

import kopf
import psycopg
import pytest
from psycopg import sql

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "operator"))

import datacentre  # noqa: E402  pylint: disable=wrong-import-position

PG_URL = os.environ.get("SVO_TEST_PG_URL")

pytestmark = pytest.mark.skipif(not PG_URL, reason="SVO_TEST_PG_URL is not set")


def _admin(dbname: str = "postgres") -> psycopg.Connection[Any]:
    assert PG_URL
    return psycopg.connect(PG_URL, dbname=dbname, autocommit=True)


@pytest.fixture(scope="module")
def databases() -> Iterator[List[str]]:
    """Two empty databases, as the Account Server would create them."""
    names = [f"svo_test_{uuid.uuid4().hex[:12]}" for _ in range(2)]
    with _admin() as c:
        for name in names:
            c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    yield names
    with _admin() as c:
        for name in names:
            c.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )
        # With the databases gone, nothing a project role owns or was granted
        # remains, so the roles can simply be dropped.
        for role in _made_roles:
            c.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


_made_roles: List[str] = []


def _data_centre(database: str, **overrides: Any) -> datacentre.DataCentre:
    info = psycopg.conninfo.conninfo_to_dict(PG_URL)
    block: Dict[str, Any] = {
        "host": info["host"],
        "port": int(info.get("port", 5432)),
        "database": database,
        "adminUser": info["user"],
        "adminPassword": info["password"],
    }
    block.update(overrides)
    dc = datacentre.parse_data_centre({"dataCentre": block})
    assert dc is not None
    return dc


def _new_project(dc: datacentre.DataCentre) -> datacentre.ProjectDatabase:
    pdb = datacentre.project_database(dc, f"project-{uuid.uuid4()}")
    _made_roles.append(pdb.role)
    return pdb


def _as_project(
    dc: datacentre.DataCentre, pdb: datacentre.ProjectDatabase, dbname: str = ""
) -> psycopg.Connection[Any]:
    return psycopg.connect(
        host=dc.host,
        port=dc.port,
        dbname=dbname or dc.database,
        user=pdb.role,
        password=pdb.secret,
        autocommit=True,
        # The cartridge image initialises its databases as SQL_ASCII, under
        # which psycopg returns text as bytes.
        client_encoding="utf8",
    )


def test_project_can_use_its_schema_and_the_cartridge(databases: List[str]) -> None:
    dc = _data_centre(databases[0])
    pdb = _new_project(dc)
    datacentre.ensure_project_database(dc, pdb)

    with _as_project(dc, pdb) as c:
        # Unqualified names land in the project's schema (search_path).
        c.execute("CREATE TABLE t (id int)")
        row = c.execute(
            "SELECT table_schema FROM information_schema.tables WHERE table_name = 't'"
        ).fetchone()
        assert row == (pdb.schema,)
        # The cartridge is reachable from the project's schema.
        smiles = c.execute("SELECT mol_from_smiles('c1ccccc1O')::text").fetchone()
        assert smiles == ("Oc1ccccc1",)


def test_limits_are_set_on_the_role(databases: List[str]) -> None:
    dc = _data_centre(
        databases[0],
        limits={"statementTimeout": 7, "tempFileLimitMB": 3, "connectionLimit": 2},
    )
    pdb = _new_project(dc)
    datacentre.ensure_project_database(dc, pdb)

    with _as_project(dc, pdb) as c:
        assert c.execute("SHOW statement_timeout").fetchone() == ("7s",)
        assert c.execute("SHOW temp_file_limit").fetchone() == ("3MB",)
    with _admin() as c:
        row = c.execute(
            "SELECT rolconnlimit FROM pg_roles WHERE rolname = %s", (pdb.role,)
        ).fetchone()
        assert row == (2,)


def test_ensure_is_idempotent_and_keeps_existing_tables(databases: List[str]) -> None:
    dc = _data_centre(databases[0])
    pdb = _new_project(dc)
    datacentre.ensure_project_database(dc, pdb)
    with _as_project(dc, pdb) as c:
        c.execute("CREATE TABLE kept (id int)")
        c.execute("INSERT INTO kept VALUES (1)")

    datacentre.ensure_project_database(dc, pdb)

    with _as_project(dc, pdb) as c:
        assert c.execute("SELECT count(*) FROM kept").fetchone() == (1,)


def test_ensure_restores_a_password_changed_by_hand(databases: List[str]) -> None:
    dc = _data_centre(databases[0])
    pdb = _new_project(dc)
    datacentre.ensure_project_database(dc, pdb)
    with _admin() as c:
        c.execute(
            sql.SQL("ALTER ROLE {} PASSWORD 'something-else'").format(
                sql.Identifier(pdb.role)
            )
        )

    datacentre.ensure_project_database(dc, pdb)

    with _as_project(dc, pdb) as c:
        assert c.execute("SELECT 1").fetchone() == (1,)


def test_a_project_cannot_see_another_projects_schema(databases: List[str]) -> None:
    dc = _data_centre(databases[0])
    mine, theirs = _new_project(dc), _new_project(dc)
    datacentre.ensure_project_database(dc, mine)
    datacentre.ensure_project_database(dc, theirs)
    with _as_project(dc, theirs) as c:
        c.execute("CREATE TABLE secret (v text)")

    with _as_project(dc, mine) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute(
                sql.SQL("SELECT * FROM {}.secret").format(sql.Identifier(theirs.schema))
            )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute(
                sql.SQL("CREATE TABLE {}.planted (v text)").format(
                    sql.Identifier(theirs.schema)
                )
            )


def test_a_project_cannot_connect_to_another_organisations_database(
    databases: List[str],
) -> None:
    mine_dc, theirs_dc = _data_centre(databases[0]), _data_centre(databases[1])
    mine = _new_project(mine_dc)
    datacentre.ensure_project_database(mine_dc, mine)
    # A launch in the other organisation is what revokes PUBLIC's CONNECT there.
    datacentre.ensure_project_database(theirs_dc, _new_project(theirs_dc))

    with pytest.raises(psycopg.OperationalError, match="permission denied"):
        _as_project(mine_dc, mine, dbname=databases[1])


def test_concurrent_launches_of_one_project_both_succeed(databases: List[str]) -> None:
    dc = _data_centre(databases[0])
    pdb = _new_project(dc)
    errors: List[BaseException] = []

    def launch() -> None:
        try:
            datacentre.ensure_project_database(dc, pdb)
        except BaseException as exc:  # pylint: disable=broad-exception-caught
            errors.append(exc)

    threads = [threading.Thread(target=launch) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    with _as_project(dc, pdb) as c:
        assert c.execute("SELECT 1").fetchone() == (1,)


def test_a_missing_database_is_a_temporary_error(databases: List[str]) -> None:
    dc = _data_centre(f"{databases[0]}_not_created")
    with pytest.raises(kopf.TemporaryError, match="not_created"):
        datacentre.ensure_project_database(dc, _new_project(dc))


def test_a_wrong_admin_password_is_a_temporary_error_that_hides_it(
    databases: List[str],
) -> None:
    dc = _data_centre(databases[0], adminPassword="wrong-password-xyz")
    with pytest.raises(kopf.TemporaryError) as raised:
        datacentre.ensure_project_database(dc, _new_project(dc))
    assert "wrong-password-xyz" not in str(raised.value)
