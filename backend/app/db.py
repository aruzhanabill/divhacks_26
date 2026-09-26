from __future__ import annotations

import os
from pathlib import Path

from psycopg_pool import ConnectionPool


def _apply_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"'))


def _load_local_env() -> None:
    backend_dir = Path(__file__).resolve().parents[1]
    repo_root = backend_dir.parent
    _apply_env_file(backend_dir / ".env")
    for creds in sorted(repo_root.glob("tiger-cloud-*-credentials.env")):
        _apply_env_file(creds)


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL") or os.environ.get("TIMESCALE_SERVICE_URL")
    if not url:
        return "postgresql://localhost:5432/safepath"
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://") :]
    return url


_load_local_env()

DATABASE_URL = _database_url()
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
