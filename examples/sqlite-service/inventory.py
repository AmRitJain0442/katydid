"""Transactional inventory behavior exercised against the run-owned SQLite database."""

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class OrderResult:
    order_id: int
    status: str
    total_cents: int
    failure_reason: str | None = None


def place_order(
    database: Path,
    *,
    run_id: str,
    customer: str,
    sku: str,
    quantity: int,
) -> OrderResult:
    """Confirm an in-stock order or persist a rejection without consuming stock."""
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if not customer.strip():
        raise ValueError("customer is required")
    connection = sqlite3.connect(database, timeout=5, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        environment_run = connection.execute(
            "SELECT run_id FROM environment_metadata WHERE singleton = 1"
        ).fetchone()
        if environment_run != (run_id,):
            connection.rollback()
            raise ValueError("order run_id does not own this environment")
        product = connection.execute(
            "SELECT unit_cents, available FROM products WHERE sku = ?", (sku,)
        ).fetchone()
        if product is None:
            connection.rollback()
            raise ValueError(f"unknown sku: {sku}")
        unit_cents, available = product
        if available < quantity:
            cursor = connection.execute(
                """
                INSERT INTO orders(run_id, customer, sku, quantity, status, failure_reason)
                VALUES (?, ?, ?, ?, 'rejected', 'insufficient-stock')
                """,
                (run_id, customer, sku, quantity),
            )
            order_id = cursor.lastrowid
            if order_id is None:
                connection.rollback()
                raise RuntimeError("SQLite did not assign an order ID")
            connection.commit()
            return OrderResult(order_id, "rejected", 0, "insufficient-stock")

        updated = connection.execute(
            "UPDATE products SET available = available - ? WHERE sku = ? AND available >= ?",
            (quantity, sku, quantity),
        )
        if updated.rowcount != 1:
            connection.rollback()
            raise RuntimeError("inventory changed concurrently")
        cursor = connection.execute(
            """
            INSERT INTO orders(run_id, customer, sku, quantity, status, failure_reason)
            VALUES (?, ?, ?, ?, 'confirmed', NULL)
            """,
            (run_id, customer, sku, quantity),
        )
        order_id = cursor.lastrowid
        if order_id is None:
            connection.rollback()
            raise RuntimeError("SQLite did not assign an order ID")
        connection.execute(
            """
            INSERT INTO order_lines(order_id, sku, quantity, unit_cents)
            VALUES (?, ?, ?, ?)
            """,
            (order_id, sku, quantity, unit_cents),
        )
        connection.commit()
        return OrderResult(order_id, "confirmed", unit_cents * quantity)
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
