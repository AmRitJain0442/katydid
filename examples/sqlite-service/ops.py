"""Owned SQLite environment preparation, readiness, and cleanup commands."""

import argparse
import json
import os
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path

DATABASE_NAME = "app.sqlite"
OWNERSHIP_NAME = "katydid-environment.json"
SQLITE_FILES = (
    DATABASE_NAME,
    f"{DATABASE_NAME}-wal",
    f"{DATABASE_NAME}-shm",
    f"{DATABASE_NAME}-journal",
)
SCHEMA_VERSION = 1
SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE environment_metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    run_id TEXT NOT NULL UNIQUE
);
CREATE TABLE products (
    sku TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    unit_cents INTEGER NOT NULL CHECK (unit_cents > 0),
    available INTEGER NOT NULL CHECK (available >= 0)
);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    customer TEXT NOT NULL,
    sku TEXT NOT NULL REFERENCES products(sku),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    status TEXT NOT NULL CHECK (status IN ('confirmed', 'rejected')),
    failure_reason TEXT,
    CHECK (
        (status = 'confirmed' AND failure_reason IS NULL)
        OR (status = 'rejected' AND failure_reason IS NOT NULL)
    )
);
CREATE TABLE order_lines (
    order_id INTEGER PRIMARY KEY REFERENCES orders(id) ON DELETE CASCADE,
    sku TEXT NOT NULL REFERENCES products(sku),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_cents INTEGER NOT NULL CHECK (unit_cents > 0)
);
PRAGMA user_version = 1;
"""
PRODUCTS = (
    ("FIELD-NOTES", "Field Notes Set", 1800, 12),
    ("ALPINE-MUG", "Alpine Mug", 2400, 8),
    ("SIGNAL-TOTE", "Signal Tote", 3200, 5),
)


class EnvironmentError(RuntimeError):
    """The command cannot prove that the environment belongs to this run."""


def _is_link(path: Path) -> bool:
    information = path.lstat()
    attributes = getattr(information, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return path.is_symlink() or bool(attributes & reparse)


def _run_id() -> str:
    value = os.environ.get("KATYDID_RUN_ID", "")
    if not value or len(value) > 128 or any(character.isspace() for character in value):
        raise EnvironmentError("KATYDID_RUN_ID is missing or invalid")
    return value


def _environment(argument: str) -> Path:
    configured = os.environ.get("KATYDID_ENVIRONMENT_DIR")
    if not configured:
        raise EnvironmentError("KATYDID_ENVIRONMENT_DIR is required")
    try:
        argument_path = Path(argument)
        configured_path = Path(configured)
        if _is_link(argument_path) or _is_link(configured_path):
            raise EnvironmentError("Environment directory cannot be a symlink or reparse point")
        argument_path = argument_path.resolve(strict=True)
        configured_path = configured_path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise EnvironmentError("Environment directory must already exist") from exc
    if argument_path != configured_path:
        raise EnvironmentError("Environment argv path does not match KATYDID_ENVIRONMENT_DIR")
    if not argument_path.is_dir():
        raise EnvironmentError("Environment path must be a directory")
    return argument_path


def _ownership(environment: Path) -> None:
    marker = environment / OWNERSHIP_NAME
    if _is_link(marker) or not stat.S_ISREG(marker.lstat().st_mode):
        raise EnvironmentError("Environment ownership marker must be a regular file")
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EnvironmentError("Environment ownership marker is unreadable") from exc
    expected = {
        "schema_version": SCHEMA_VERSION,
        "run_id": _run_id(),
        "environment": str(environment),
    }
    if data != expected:
        raise EnvironmentError("Environment ownership marker does not match this run")


def _write_ownership(environment: Path) -> None:
    marker = environment / OWNERSHIP_NAME
    if marker.exists() or marker.is_symlink():
        raise EnvironmentError("Environment ownership marker already exists")
    value = {
        "schema_version": SCHEMA_VERSION,
        "run_id": _run_id(),
        "environment": str(environment),
    }
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=environment,
            prefix="katydid-environment-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, marker)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _database(environment: Path, *, must_exist: bool) -> Path:
    database = environment / DATABASE_NAME
    if database.exists() or database.is_symlink():
        if _is_link(database) or not stat.S_ISREG(database.lstat().st_mode):
            raise EnvironmentError("SQLite database must be a regular owned file")
    elif must_exist:
        raise EnvironmentError("SQLite database does not exist")
    return database


def prepare(environment: Path) -> None:
    for filename in SQLITE_FILES:
        path = environment / filename
        if path.exists() or path.is_symlink():
            raise EnvironmentError(f"Refusing to replace existing environment file: {filename}")
    _write_ownership(environment)
    database = _database(environment, must_exist=False)
    with sqlite3.connect(database, timeout=5) as connection:
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.executescript(SCHEMA)
        connection.execute(
            "INSERT INTO environment_metadata(singleton, run_id) VALUES (1, ?)",
            (_run_id(),),
        )
        connection.executemany(
            "INSERT INTO products(sku, name, unit_cents, available) VALUES (?, ?, ?, ?)",
            PRODUCTS,
        )


def ready(environment: Path) -> None:
    _ownership(environment)
    database = _database(environment, must_exist=True)
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=2)
    try:
        if connection.execute("PRAGMA user_version").fetchone() != (SCHEMA_VERSION,):
            raise EnvironmentError("SQLite schema version is not ready")
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise EnvironmentError("SQLite integrity check failed")
        run = connection.execute("SELECT run_id FROM environment_metadata").fetchone()
        if run != (_run_id(),):
            raise EnvironmentError("SQLite run identity does not match")
        seeded = connection.execute("SELECT COUNT(*), SUM(available) FROM products").fetchone()
        if seeded != (len(PRODUCTS), sum(product[3] for product in PRODUCTS)):
            raise EnvironmentError("SQLite seed data is not ready")
    finally:
        connection.close()


def cleanup(environment: Path) -> None:
    _ownership(environment)
    for filename in SQLITE_FILES:
        path = environment / filename
        try:
            information = path.lstat()
        except FileNotFoundError:
            continue
        if _is_link(path) or not stat.S_ISREG(information.st_mode):
            raise EnvironmentError(f"Refusing to remove non-regular environment file: {filename}")
        path.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("prepare", "ready", "cleanup"))
    parser.add_argument("environment")
    arguments = parser.parse_args(argv)
    try:
        environment = _environment(arguments.environment)
        operations = {"prepare": prepare, "ready": ready, "cleanup": cleanup}
        operations[arguments.operation](environment)
        return 0
    except (EnvironmentError, OSError, sqlite3.Error) as exc:
        print(f"{arguments.operation} failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
