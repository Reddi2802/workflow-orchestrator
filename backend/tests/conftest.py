# tests/conftest.py
"""
Shared pytest fixtures. Combines two independently-built pieces:

- Paramash's fixtures: table setup, per-test cleanup via a committed
  delete, and a FastAPI TestClient — used by his CRUD endpoint tests.
- Hridhayansh's db_session fixture: a transaction that's rolled back
  after each test — used by the scheduler integration tests, which need
  finer-grained isolation (creating/inspecting rows mid-test) than a
  blanket "wipe workflows before every test" approach gives.

Both fixtures run against the SAME real Docker Postgres container — no
mocking, no sqlite substitute — consistent with this project's "verify
at the database level" standard. If a new set of tests needs yet another
DB access pattern, extend this file, don't create a second conftest.py
somewhere else.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.database import Base, SessionLocal, engine
from app.main import app


@pytest.fixture(scope="session", autouse=True)
def _create_tables():
    """
    Tables should already exist via `alembic upgrade head` before you run
    these tests (that's how the real app will run). This is just a safety
    net in case they don't — create_all is a no-op on tables that already
    exist.
    """
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture(autouse=True)
def _clean_db():
    """Wipe workflows (and everything cascading from it) before each test
    so tests don't interfere with each other."""
    db = SessionLocal()
    try:
        from app.models.entities import Workflow

        db.query(Workflow).delete()
        db.commit()
    finally:
        db.close()
    yield


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def db_session():
    """
    A DB session scoped to a single transaction, rolled back after the
    test — used by tests that need to create rows and inspect query
    results within the same test without those rows persisting, even
    past the blanket _clean_db wipe above.
    """
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)

    yield session

    session.close()
    transaction.rollback()
    connection.close()
