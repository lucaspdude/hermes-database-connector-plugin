"""Unit tests for the SQLite store (``service.py``).

They run against a throwaway SQLite file in ``tmp_path`` — no Hermes runtime,
no ``plugin_db`` — which is exactly why ``init_store`` imports it lazily.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import service

BASE = datetime(2025, 1, 1, tzinfo=timezone.utc)


def _ts(offset_seconds: int) -> str:
    return (BASE + timedelta(seconds=offset_seconds)).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _connection(**overrides):
    payload = {
        "id": "prod-sqlserver",
        "label": "Produção - SQL Server",
        "engine": "sqlserver",
        "environment": "prod",
        "host": "sql.prod.local",
        "port": 1433,
        "database": "HubFinanceira",
        "username": "hermes_ro",
        "secret_ref": "DB_PROD_SQLSERVER_PASSWORD",
    }
    payload.update(overrides)
    return payload


@pytest.fixture()
def db(tmp_path):
    path = str(tmp_path / "connector.db")
    conn = service.open_store(path)
    yield conn
    conn.close()


# --------------------------------------------------------------------------
# 2.1 migração versionada e idempotente
# --------------------------------------------------------------------------


def test_migrate_is_idempotent_and_creates_full_schema(tmp_path):
    path = str(tmp_path / "connector.db")
    conn = sqlite3.connect(path)
    service.migrate(conn)
    service.migrate(conn)  # segunda execução: no-op

    assert service.schema_version(conn) == 1

    connections = {row[1] for row in conn.execute("PRAGMA table_info(connections)")}
    assert connections == set(service.CONNECTION_COLUMNS)

    log = {row[1] for row in conn.execute("PRAGMA table_info(query_log)")}
    assert log == {
        "id",
        "ts",
        "query_id",
        "connection_id",
        "engine",
        "environment",
        "statement",
        "rows",
        "duration_ms",
        "status",
        "error",
        "source",
        "session_id",
        "conversation_id",
    }

    indexes = {row[1] for row in conn.execute("PRAGMA index_list(query_log)")}
    assert {"ix_query_log_ts", "ix_query_log_conn"} <= indexes

    pk = [row for row in conn.execute("PRAGMA table_info(connections)") if row[5]]
    assert [row[1] for row in pk] == ["id"]

    fks = list(conn.execute("PRAGMA foreign_key_list(query_log)"))
    assert fks and fks[0]["table"] == "connections"
    assert fks[0]["on_delete"].upper() == "CASCADE"

    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    conn.close()


# --------------------------------------------------------------------------
# 2.2 CRUD de conexões
# --------------------------------------------------------------------------


def test_crud_create_list_upsert_delete_cascades_log(db):
    created = service.upsert_connection(db, _connection())
    assert created["read_only"] == 1
    assert created["enabled"] == 1
    assert created["options_json"] == "{}"
    assert created["created_at"].endswith("Z") and created["updated_at"].endswith("Z")

    assert [row["id"] for row in service.list_connections(db)] == ["prod-sqlserver"]

    service.append_query_log(
        db,
        connection_id="prod-sqlserver",
        engine="sqlserver",
        environment="prod",
        statement="SELECT 1",
        source="ui",
    )
    assert service.count_query_log(db) == 1

    updated = service.upsert_connection(db, _connection(label="Produção II", port=1434))
    assert updated["label"] == "Produção II"
    assert updated["port"] == 1434
    assert updated["created_at"] == created["created_at"]
    assert len(service.list_connections(db)) == 1  # upsert não duplica

    assert service.delete_connection(db, "prod-sqlserver") is True
    assert service.list_connections(db) == []
    assert service.count_query_log(db) == 0  # cascata


@pytest.mark.parametrize(
    "field, value",
    [("engine", "oracle"), ("environment", "staging"), ("engine", None), ("environment", 42)],
)
def test_invalid_engine_or_environment_is_rejected_without_writing(db, field, value):
    with pytest.raises(service.ValidationError) as excinfo:
        service.upsert_connection(db, _connection(**{field: value}))

    assert "invalid" in str(excinfo.value)
    assert service.list_connections(db) == []


# --------------------------------------------------------------------------
# 2.3 resolve_secret: três estados
# --------------------------------------------------------------------------


def test_resolve_secret_resolved_missing_and_unscoped(db):
    class UnscopedSecretError(Exception):
        pass

    def resolver(name):
        if name == "DB_PRESENT":
            return "pw-resolved-123"
        if name == "DB_EMPTY":
            return ""
        raise UnscopedSecretError("no secret scope for active profile")

    resolved = service.resolve_secret({"secret_ref": "DB_PRESENT"}, resolver=resolver)
    assert resolved["state"] == "resolved"
    assert resolved["value"] == "pw-resolved-123"

    missing = service.resolve_secret({"secret_ref": "DB_EMPTY"}, resolver=resolver)
    assert missing["state"] == "missing"
    assert missing["value"] is None

    unscoped = service.resolve_secret({"secret_ref": "DB_OTHER"}, resolver=resolver)
    assert unscoped["state"] == "unscoped"
    assert unscoped["value"] is None


# --------------------------------------------------------------------------
# 2.4 nenhum valor de segredo persistido
# --------------------------------------------------------------------------


def test_no_secret_value_is_written_to_the_database_file(tmp_path):
    password = "NaoPercaEstaSenha-9XyZ"
    path = str(tmp_path / "connector.db")
    conn = service.open_store(path)

    with pytest.raises(service.ValidationError):
        service.upsert_connection(conn, _connection(password=password))

    service.upsert_connection(conn, _connection())
    service.resolve_secret({"secret_ref": "DB_PROD_SQLSERVER_PASSWORD"}, resolver=lambda _n: password)
    service.append_query_log(
        conn,
        connection_id="prod-sqlserver",
        engine="sqlserver",
        environment="prod",
        statement=None,
        source="ui",
        error=None,
    )
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()

    raw = open(path, "rb").read()
    for suffix in ("-wal", "-shm"):
        extra = tmp_path / f"connector.db{suffix}"
        if extra.exists():
            raw += extra.read_bytes()
    assert password.encode() not in raw

    reopened = service.open_store(path)
    row = service.get_connection(reopened, "prod-sqlserver")
    assert row["secret_ref"] == "DB_PROD_SQLSERVER_PASSWORD"
    assert "value" not in row and "password" not in row
    reopened.close()


# --------------------------------------------------------------------------
# 2.5 poda do query_log com cap de 1.000 linhas
# --------------------------------------------------------------------------


def test_query_log_prunes_to_cap_keeping_the_newest(db):
    service.upsert_connection(db, _connection())

    ids = []
    for index in range(1005):
        query_id = f"q-{index:04d}"
        ids.append(query_id)
        service.append_query_log(
            db,
            query_id=query_id,
            connection_id="prod-sqlserver",
            engine="sqlserver",
            environment="prod",
            statement=None,
            source="agent",
            ts=_ts(index),
        )

    assert service.count_query_log(db) == 1000
    kept = {row["query_id"] for row in service.list_query_log(db, limit=2000)}
    # os 5 mais antigos saem; os 1.000 mais recentes sobrevivem
    assert not kept.intersection(ids[:5])
    assert set(ids[5:]) == kept
    assert "q-1004" in kept
    assert service.count_query_log(db) <= service.QUERY_LOG_CAP


# --------------------------------------------------------------------------
# escritas concorrentes (spec connection-store): REST + tool ao mesmo tempo
# --------------------------------------------------------------------------


def test_concurrent_writes_keep_every_row_and_corrupt_nothing(tmp_path):
    """8 escritores x 50 + 4 leitores: 400 linhas, zero erro (spec: REST **e** tool).

    The readers matter as much as the writers: ``GET /queries`` / ``GET /status``
    poll the store while the agent tools write, which is exactly the mix that
    used to corrupt the shared connection (and lose writes).
    """
    import threading

    path = str(tmp_path / "connector.db")
    store = service.open_store(path)
    service.upsert_connection(store, _connection())

    writers = 8
    per_writer = 50
    readers = 4
    errors: list[str] = []

    def worker(index: int) -> None:
        try:
            for step in range(per_writer):
                service.append_query_log(
                    store,  # mesma store injetada pelas rotas
                    connection_id="prod-sqlserver",
                    engine="sqlserver",
                    environment="prod",
                    statement=f"SELECT col{index} FROM t{step}",
                    source="agent" if index % 2 else "ui",
                    ts=_ts(index * per_writer + step),
                )
        except Exception as exc:  # noqa: BLE001 - registrado e assertion abaixo
            errors.append(f"writer {index}: {type(exc).__name__}: {exc}")

    def reader(index: int) -> None:
        """Polling da UI: o que ``GET /queries`` e ``GET /status`` chamam."""
        try:
            for _ in range(25):
                service.list_query_log(store, limit=100)
                service.status_snapshot(store, limit=20)
        except Exception as exc:  # noqa: BLE001 - registrado e assertion abaixo
            errors.append(f"reader {index}: {type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(writers)]
    threads += [threading.Thread(target=reader, args=(index,)) for index in range(readers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)

    assert errors == [], errors
    assert service.count_query_log(store) == writers * per_writer
    assert len({row["query_id"] for row in service.list_query_log(store, limit=1000)}) == (
        writers * per_writer
    )

    # o arquivo continua integro e legivel por uma conexao independente
    reopened = sqlite3.connect(path)
    try:
        assert reopened.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert reopened.execute("SELECT COUNT(*) FROM query_log").fetchone()[0] == (
            writers * per_writer
        )
    finally:
        reopened.close()
        store.close()


def test_wiring_keeps_the_path_as_the_canonical_store(tmp_path):
    """Depois do wiring a store e o **caminho**: a conexao compartilhada sai de cena.

    Root cause of the lost writes: ``configure``/``set_store`` used to hand the
    very same ``sqlite3.Connection`` to every request, and each operation asked
    *that* object for its path (``PRAGMA database_list``) from every thread.
    """
    import os

    from dashboard import plugin_api

    path = str(tmp_path / "connector.db")
    conn = service.open_store(path)
    plugin_api.set_store(conn)
    try:
        store = plugin_api.get_store()
        assert isinstance(store, str), store
        assert store == os.path.realpath(path)
        assert plugin_api.get_store() == store  # resolvido uma vez, nao por chamada

        # leitura e escrita concorrentes pelo caminho canonico: nada compartilhado
        import threading

        errors: list[str] = []
        service.upsert_connection(store, _connection())

        def write(index: int) -> None:
            try:
                for step in range(25):
                    service.append_query_log(
                        store,
                        connection_id="prod-sqlserver",
                        engine="sqlserver",
                        environment="prod",
                        statement=f"SELECT {index} FROM t{step}",
                        source="ui",
                        ts=_ts(index * 25 + step),
                    )
            except Exception as exc:  # noqa: BLE001 - assertion abaixo
                errors.append(f"writer {index}: {type(exc).__name__}: {exc}")

        def read(index: int) -> None:
            try:
                for _ in range(25):
                    service.list_query_log(store, limit=50)
                    service.status_snapshot(store)
            except Exception as exc:  # noqa: BLE001 - assertion abaixo
                errors.append(f"reader {index}: {type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=write, args=(i,)) for i in range(4)]
        threads += [threading.Thread(target=read, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)

        assert errors == [], errors
        assert service.count_query_log(store) == 100
    finally:
        plugin_api.set_store(None)
        conn.close()


def test_start_and_finish_write_a_single_log_line(db):
    service.upsert_connection(db, _connection())

    query_id = service.start_query_log(
        db,
        connection_id="prod-sqlserver",
        engine="sqlserver",
        environment="prod",
        statement="SELECT id FROM t WHERE id = 42",
        source="agent",
        ts=_ts(0),
    )
    assert db.execute(
        "SELECT status FROM query_log WHERE query_id = ?", (query_id,)
    ).fetchone()[0] == "running"

    assert service.finish_query_log(db, query_id, status="ok", rows=3, duration_ms=12)
    assert service.count_query_log(db) == 1

    row = service.list_query_log(db)[0]
    assert row["status"] == "ok"
    assert row["rows"] == 3
    assert "?" in row["statement"] and "42" not in row["statement"]
