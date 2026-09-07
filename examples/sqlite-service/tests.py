"""Business tests using the SQLite database owned by this Katydid run."""

import json
import os
import sqlite3
from pathlib import Path

import pytest
from inventory import place_order


def environment() -> tuple[Path, Path, str]:
    root = Path(os.environ["KATYDID_ENVIRONMENT_DIR"]).resolve(strict=True)
    run_id = os.environ["KATYDID_RUN_ID"]
    marker = json.loads((root / "katydid-environment.json").read_text(encoding="utf-8"))
    assert marker == {"schema_version": 1, "run_id": run_id, "environment": str(root)}
    database = (root / "app.sqlite").resolve(strict=True)
    assert database.parent == root
    return root, database, run_id


def test_confirmed_order_decrements_stock_and_records_line_total() -> None:
    _root, database, run_id = environment()
    result = place_order(
        database,
        run_id=run_id,
        customer="field@example.test",
        sku="ALPINE-MUG",
        quantity=2,
    )
    assert result.status == "confirmed"
    assert result.total_cents == 4800
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT available FROM products WHERE sku = 'ALPINE-MUG'"
        ).fetchone() == (6,)
        assert connection.execute(
            "SELECT sku, quantity, unit_cents FROM order_lines WHERE order_id = ?",
            (result.order_id,),
        ).fetchone() == ("ALPINE-MUG", 2, 2400)


def test_rejected_order_preserves_stock_and_records_failure_state() -> None:
    _root, database, run_id = environment()
    with sqlite3.connect(database) as connection:
        before = connection.execute(
            "SELECT available FROM products WHERE sku = 'SIGNAL-TOTE'"
        ).fetchone()
    result = place_order(
        database,
        run_id=run_id,
        customer="overpack@example.test",
        sku="SIGNAL-TOTE",
        quantity=99,
    )
    assert result.status == "rejected"
    assert result.failure_reason == "insufficient-stock"
    assert result.total_cents == 0
    with sqlite3.connect(database) as connection:
        after = connection.execute(
            "SELECT available FROM products WHERE sku = 'SIGNAL-TOTE'"
        ).fetchone()
        order = connection.execute(
            "SELECT status, failure_reason FROM orders WHERE id = ?", (result.order_id,)
        ).fetchone()
        lines = connection.execute(
            "SELECT COUNT(*) FROM order_lines WHERE order_id = ?", (result.order_id,)
        ).fetchone()
    assert after == before
    assert order == ("rejected", "insufficient-stock")
    assert lines == (0,)


def test_database_contains_only_this_run_identity_and_fresh_seed() -> None:
    root, database, run_id = environment()
    assert database == root / "app.sqlite"
    with pytest.raises(ValueError, match="does not own"):
        place_order(
            database,
            run_id="another-run",
            customer="intruder@example.test",
            sku="FIELD-NOTES",
            quantity=1,
        )
    with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT run_id FROM environment_metadata").fetchall() == [
            (run_id,)
        ]
        assert connection.execute(
            "SELECT COUNT(*) FROM orders WHERE run_id != ?", (run_id,)
        ).fetchone() == (0,)
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
