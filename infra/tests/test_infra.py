"""Infra smoke tests for the Docker Compose data layer (Task 1).

These tests assert real behavior against the live stack — real connections to
MySQL / MinIO / Redis, no mocks. They require the compose stack to be up:

    docker compose -f infra/docker-compose.yml up -d --wait
    uv run pytest infra/tests -m infra

Defaults mirror the defaults in infra/docker-compose.yml (and
infra/.env.example); environment variables from the root .env override them.
"""

import os

import httpx
import pytest
import redis
from minio import Minio
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.infra

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "mysql+pymysql://hr_user:hr_dev_pw@127.0.0.1:3306/hr_workbuddy",
)
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "127.0.0.1:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "minioadmin")
MINIO_BUCKET = "hr-workbuddy"  # infra 契约的一部分，由 minio-init 创建


def test_mysql_reachable_and_database_exists():
    """SQLAlchemy (PyMySQL) can connect as the non-root user to hr_workbuddy."""
    engine = create_engine(DATABASE_URL)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar_one() == 1
            assert conn.execute(text("SELECT DATABASE()")).scalar_one() == "hr_workbuddy"
    finally:
        engine.dispose()


def test_minio_live_endpoint():
    """MinIO health/live endpoint answers 200."""
    resp = httpx.get(f"http://{MINIO_ENDPOINT}/minio/health/live")
    assert resp.status_code == 200


def test_minio_bucket_exists():
    """The hr-workbuddy bucket was created by the minio-init container."""
    client = Minio(
        MINIO_ENDPOINT,
        access_key=MINIO_ACCESS_KEY,
        secret_key=MINIO_SECRET_KEY,
        secure=False,
    )
    assert client.bucket_exists(MINIO_BUCKET)


def test_redis_set_get():
    """Redis accepts a SET and returns the value on GET."""
    client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    key = "infra:test:set_get"
    try:
        client.set(key, "ok")
        assert client.get(key) == "ok"
    finally:
        client.delete(key)
        client.close()
