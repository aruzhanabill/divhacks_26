from __future__ import annotations

import os
from pathlib import Path

from psycopg_pool import ConnectionPool


def _load_local_env() -> None:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"'))


_load_local_env()

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost:5432/safepath")
SCHEMA_PATH = Path(__file__).with_name("schema.sql")

pool = ConnectionPool(conninfo=DATABASE_URL, min_size=1, max_size=5, open=False, kwargs={"autocommit": True})


def init_db() -> None:
    pool.open()
    script = SCHEMA_PATH.read_text()
    statements = [part.strip() for part in script.split(";") if part.strip()]
    with pool.connection() as conn:
        for statement in statements:
            conn.execute(statement)


def close_db() -> None:
    pool.close()
