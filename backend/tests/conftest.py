# tests/conftest.py
"""
Shared pytest fixtures. Combines three independently-built pieces:

- Paramash's fixtures: table setup, per-test cleanup via a committed
  delete, and a FastAPI TestClient — used by his CRUD endpoint tests.
- Hridhayansh's db_session fixture: a transaction that's rolled back
  after each test — used by the scheduler integration tests, which need
  finer-grained isolation (creating/inspecting rows mid-test) than a
  blanket "wipe workflows before every test" approach gives.
- Hridhayansh's _clean_queue fixture (Week 5): Postgres gets wiped before
  every test via _clean_db below, but nothing wiped the Redis queue --
  found when test_recovery.py's tests failed with ids that didn't match
  what they'd just enqueued. LPUSH/BRPOP is FIFO, so a leftover id from
  an earlier test run (or from manually running the app against the same
  Redis instance) gets dequeued BEFORE anything the current test just
  pushed, silently handing a test someone else's stale id. Affects any
  test file that touches the real queue, not just test_recovery.py --
  test_worker_pool.py had the same latent gap, it just never got exposed.

Both DB fixtures run against the SAME real Docker Postgres container, and
_clean_queue runs against the same real Redis container -- no mocking, no
in-memory substitute, consistent with this project's "verify at the
database level" standard. If a new set of tests needs yet another access
pattern, extend this file, don't create a second conftest.py somewhere else.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.database import Base, SessionLocal, engine
from app.main import app
from app.queue.redis_queue import TASK_QUEUE_KEY, redis_client


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


@pytest.fixture(autouse=True)
def _clean_queue():
    """Wipe the Redis task queue before each test -- the Postgres
    equivalent of _clean_db above. Without this, a stale task_run_id left
    over from an earlier test run (or from manually running the live app
    against the same Redis instance) sits in the queue and gets dequeued
    FIRST by the next test that calls dequeue_task, since LPUSH/BRPOP is
    FIFO. A test then silently receives someone else's id instead of its
    own, which looks like a logic bug in whatever's under test but isn't.

    Caution for local dev: this deletes the WHOLE task_queue key, so don't
    run the live app (uvicorn) against the same Redis instance while the
    test suite is running -- it'll wipe out real in-flight task_run_ids
    the live app's workers are waiting to dequeue. Stick to one or the
    other against this Redis instance at a time.
    """
    redis_client.delete(TASK_QUEUE_KEY)
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
