# tests/test_redis_queue.py
import pytest
import redis

from app.queue.redis_queue import redis_client, TASK_QUEUE_KEY, enqueue_task, dequeue_task


@pytest.fixture(autouse=True)
def clean_queue():
    """Ensure the queue key is empty before and after each test,
    so leftover ids from a previous run can't cause false results."""
    redis_client.delete(TASK_QUEUE_KEY)
    yield
    redis_client.delete(TASK_QUEUE_KEY)


def test_enqueue_then_dequeue_returns_same_id():
    enqueue_task(42)
    result = dequeue_task(timeout_seconds=1)
    assert result == 42


def test_dequeue_on_empty_queue_returns_none_on_timeout():
    result = dequeue_task(timeout_seconds=1)
    assert result is None

def test_dequeue_returns_none_on_client_timeout_error(monkeypatch):
    """Guards against the Week 4 bug: brpop raising TimeoutError instead
    of returning None on an empty queue must still surface as None."""
    def raise_timeout(*args, **kwargs):
        raise redis.exceptions.TimeoutError("simulated client-side timeout")

    monkeypatch.setattr(redis_client, "brpop", raise_timeout)
    assert dequeue_task(timeout_seconds=1) is None