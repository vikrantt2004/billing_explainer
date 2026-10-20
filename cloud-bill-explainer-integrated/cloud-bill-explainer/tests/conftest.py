import hashlib
import sqlite3

import pytest

from billing_explainer import repository as repo
from billing_explainer.database import connect, init_db


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    c = connect(path)
    init_db(c)
    c.close()
    return path


@pytest.fixture
def conn(db_path):
    c = connect(db_path)
    yield c
    c.close()


@pytest.fixture
def alice(conn):
    return repo.get_or_create_user(conn, "google", "sub-alice", "alice@example.com")


@pytest.fixture
def bob(conn):
    return repo.get_or_create_user(conn, "google", "sub-bob", "bob@example.com")


def make_doc(conn, user, content=b"hello", **kw):
    """Helper: register a document for `user` with a hash derived from `content`."""
    digest = hashlib.sha256(content).hexdigest()
    return repo.create_document(
        conn, user.id, original_filename=kw.pop("original_filename", "bill.csv"),
        storage_path=kw.pop("storage_path", f"{digest[:12]}.csv"), sha256=digest,
        file_type="csv", file_size_bytes=len(content), **kw)


@pytest.fixture
def settings(tmp_path):
    from billing_explainer.config import Settings
    return Settings(db_path=tmp_path / "test.db", upload_dir=tmp_path / "uploads",
                    max_upload_bytes=1024 * 1024)
