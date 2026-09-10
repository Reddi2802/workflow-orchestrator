# tests/test_dag.py
"""
has_cycle: valid chain, direct cycle, self-loop, diamond (must not
false-positive).
topological_sort: known DAG, output order respects every edge.

Uses lightweight fake objects instead of real Task/TaskDependency ORM
instances backed by a DB — these tests only need .id, .upstream_task_id,
.downstream_task_id, so a real SQLAlchemy session isn't necessary here.
If has_cycle's real callers ever depend on other Task attributes, revisit.
"""
from dataclasses import dataclass

import pytest

from app.engine.dag import has_cycle, topological_sort


@dataclass
class FakeTask:
    id: int


@dataclass
class FakeDependency:
    upstream_task_id: int
    downstream_task_id: int


def test_has_cycle_valid_chain_returns_false():
    tasks = [FakeTask(1), FakeTask(2), FakeTask(3)]
    deps = [FakeDependency(1, 2), FakeDependency(2, 3)]
    assert has_cycle(tasks, deps) is False


def test_has_cycle_direct_cycle_returns_true():
    tasks = [FakeTask(1), FakeTask(2)]
    deps = [FakeDependency(1, 2), FakeDependency(2, 1)]
    assert has_cycle(tasks, deps) is True


def test_has_cycle_self_loop_returns_true():
    tasks = [FakeTask(1)]
    deps = [FakeDependency(1, 1)]
    assert has_cycle(tasks, deps) is True


def test_has_cycle_diamond_does_not_false_positive():
    # A -> B, A -> C, B -> D, C -> D
    tasks = [FakeTask(1), FakeTask(2), FakeTask(3), FakeTask(4)]
    deps = [
        FakeDependency(1, 2),
        FakeDependency(1, 3),
        FakeDependency(2, 4),
        FakeDependency(3, 4),
    ]
    assert has_cycle(tasks, deps) is False


def test_topological_sort_respects_dependency_order():
    # A -> B, A -> C, B -> D, C -> D
    task_a, task_b, task_c, task_d = FakeTask(1), FakeTask(2), FakeTask(3), FakeTask(4)
    tasks = [task_a, task_b, task_c, task_d]
    deps = [
        FakeDependency(1, 2),
        FakeDependency(1, 3),
        FakeDependency(2, 4),
        FakeDependency(3, 4),
    ]

    order = topological_sort(tasks, deps)
    positions = {task.id: idx for idx, task in enumerate(order)}

    assert positions[1] < positions[2]
    assert positions[1] < positions[3]
    assert positions[2] < positions[4]
    assert positions[3] < positions[4]


def test_topological_sort_raises_on_cyclic_input():
    # topological_sort does not itself validate acyclicity — confirm it
    # fails loudly rather than returning a silently wrong order.
    import networkx as nx

    tasks = [FakeTask(1), FakeTask(2)]
    deps = [FakeDependency(1, 2), FakeDependency(2, 1)]

    with pytest.raises(nx.NetworkXUnfeasible):
        topological_sort(tasks, deps)
