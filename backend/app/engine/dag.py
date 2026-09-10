# app/engine/dag.py
"""
DAG construction, cycle detection, and topological ordering for workflows.

has_cycle() signature is fixed by cross-team agreement (see GitHub issue) —
Paramash's POST /workflows calls this before writing anything to the DB.
Do not change this signature without re-confirming with him first.
"""
from __future__ import annotations

import networkx as nx

from app.models.entities import Task, TaskDependency


def _build_graph(tasks: list[Task], dependencies: list[TaskDependency]) -> nx.DiGraph:
    """
    Build a directed graph: one node per Task.id, one edge per
    TaskDependency (upstream_task_id -> downstream_task_id).

    Nodes are added explicitly even if a task has no dependencies at all,
    so an isolated task still shows up in topological_sort's output.
    """
    graph = nx.DiGraph()
    graph.add_nodes_from(task.id for task in tasks)
    graph.add_edges_from(
        (dep.upstream_task_id, dep.downstream_task_id) for dep in dependencies
    )
    return graph


def has_cycle(tasks: list[Task], dependencies: list[TaskDependency]) -> bool:
    """
    Return True if the given tasks + dependencies contain at least one cycle.

    A cyclic workflow can never finish — some task would be waiting on a
    dependency chain that loops back to itself. Call this BEFORE persisting
    a workflow; reject with 400 if it returns True, write nothing to the DB.

    Confirmed non-cyclic case: diamond A->B, A->C, B->D, C->D must return False.
    Confirmed cyclic cases: direct cycle A->B->A, and self-loop A->A, must
    both return True.
    """
    graph = _build_graph(tasks, dependencies)
    return not nx.is_directed_acyclic_graph(graph)


def topological_sort(tasks: list[Task], dependencies: list[TaskDependency]) -> list[Task]:
    """
    Return tasks in a valid execution order: every task appears after all
    of its upstream dependencies.

    Internal to the worker pool / scheduler (Hridhayansh-only) — not part
    of the cross-team signature agreement, unlike has_cycle.

    Raises networkx.NetworkXUnfeasible if the graph has a cycle — callers
    are expected to have already validated with has_cycle() first. This is
    NOT a substitute for that check; it's a sort, and it will happily crash
    on a cyclic graph rather than tell you why.
    """
    graph = _build_graph(tasks, dependencies)
    ordered_ids = list(nx.topological_sort(graph))

    tasks_by_id = {task.id: task for task in tasks}
    return [tasks_by_id[task_id] for task_id in ordered_ids]
