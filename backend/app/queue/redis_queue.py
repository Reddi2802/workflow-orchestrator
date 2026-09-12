# app/queue/redis_queue.py
import redis

REDIS_URL = "redis://localhost:6379/0"

redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)

TASK_QUEUE_KEY = "task_queue"


def enqueue_task(task_run_id: int) -> None:
    """Push a task_run_id onto the queue for a worker to pick up later."""
    redis_client.lpush(TASK_QUEUE_KEY, task_run_id)


def dequeue_task(timeout_seconds: int) -> int | None:
    """Block up to timeout_seconds waiting for a task_run_id.

    Returns the id as an int, or None if nothing arrived before the
    timeout — never raises on timeout.
    """
    result = redis_client.brpop(TASK_QUEUE_KEY, timeout=timeout_seconds)
    if result is None:
        return None
    _, value = result
    return int(value)