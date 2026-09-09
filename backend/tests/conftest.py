import pytest
from fastapi.testclient import TestClient

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
