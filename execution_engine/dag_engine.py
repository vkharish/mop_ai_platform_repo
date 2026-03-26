"""
DAG Engine — dependency graph for MOP step execution.

Converts a flat list of TestSteps into a directed acyclic graph based on
the dependencies[] field (list of step_ids that must complete first).

Uses Kahn's algorithm (pure Python, no networkx) for:
  - Topological sort
  - Cycle detection
  - Execution wave generation (parallel groups)
  - Critical path calculation
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from typing import Dict, List, Optional, Set, Tuple

from models.canonical import TestStep

logger = logging.getLogger(__name__)


class DAGValidationError(Exception):
    pass


class DAGEngine:
    """
    Builds and analyses a dependency graph from a list of TestSteps.

    Usage:
        engine = DAGEngine(steps)
        engine.validate()           # raises DAGValidationError if cycle
        waves = engine.waves()      # List[List[step_id]] for parallel execution
        path  = engine.critical_path()
    """

    def __init__(self, steps: List[TestStep]) -> None:
        self._steps: Dict[str, TestStep] = {s.step_id: s for s in steps}
        # adjacency: predecessor → set of successors
        self._successors: Dict[str, Set[str]] = defaultdict(set)
        # predecessor count per node
        self._in_degree: Dict[str, int] = {s.step_id: 0 for s in steps}

        for step in steps:
            for dep in step.dependencies:
                if dep not in self._steps:
                    raise DAGValidationError(
                        f"Step '{step.step_id}' depends on unknown step '{dep}'"
                    )
                self._successors[dep].add(step.step_id)
                self._in_degree[step.step_id] += 1

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def validate(self) -> None:
        """Raise DAGValidationError if the graph contains a cycle."""
        cycles = self._find_cycles()
        if cycles:
            raise DAGValidationError(
                f"Dependency cycle detected involving steps: {cycles}"
            )

    def waves(self) -> List[List[str]]:
        """
        Return execution waves via Kahn's algorithm.

        Each wave is a list of step_ids that can run in parallel.
        All dependencies of wave N are in waves 0..N-1.
        """
        in_deg = dict(self._in_degree)
        queue: deque = deque(
            sid for sid, deg in in_deg.items() if deg == 0
        )
        result: List[List[str]] = []

        while queue:
            wave = list(queue)
            queue.clear()
            result.append(sorted(wave))  # sorted for deterministic ordering
            for sid in wave:
                for succ in self._successors.get(sid, set()):
                    in_deg[succ] -= 1
                    if in_deg[succ] == 0:
                        queue.append(succ)

        # If not all nodes were processed, there's a cycle (shouldn't reach here
        # after validate() but defensive check)
        processed = sum(len(w) for w in result)
        if processed < len(self._steps):
            raise DAGValidationError("Cycle detected during wave generation")

        logger.debug("DAG waves: %d waves for %d steps", len(result), len(self._steps))
        return result

    def critical_path(self) -> List[str]:
        """
        Return the step_ids on the longest dependency chain (by step count).
        This is the minimum number of sequential waves needed.
        """
        # dp[node] = (longest_path_length, predecessor_in_path)
        dp: Dict[str, Tuple[int, Optional[str]]] = {
            sid: (0, None) for sid in self._steps
        }
        for wave in self.waves():
            for sid in wave:
                for succ in self._successors.get(sid, set()):
                    if dp[sid][0] + 1 > dp[succ][0]:
                        dp[succ] = (dp[sid][0] + 1, sid)

        # Find end node with max length
        end_node = max(dp, key=lambda k: dp[k][0])
        path = []
        node: Optional[str] = end_node
        while node is not None:
            path.append(node)
            node = dp[node][1]
        path.reverse()
        return path

    def ready_steps(self, completed: Set[str]) -> List[str]:
        """
        Return step_ids that are ready to run given the set of completed step_ids.
        A step is ready if all its dependencies are in `completed` and it is not
        itself completed.
        """
        ready = []
        for step_id, step in self._steps.items():
            if step_id in completed:
                continue
            if all(dep in completed for dep in step.dependencies):
                ready.append(step_id)
        return ready

    def topological_order(self) -> List[str]:
        """Return a valid linear topological ordering (one of many possible)."""
        return [sid for wave in self.waves() for sid in wave]

    def estimated_duration_s(self) -> float:
        """Sum of timeout_s along the critical path — lower bound on total time."""
        path = self.critical_path()
        return sum(
            self._steps[sid].execution_policy.timeout_s
            + self._steps[sid].timing.delay_before_s
            + self._steps[sid].timing.delay_after_s
            for sid in path
            if sid in self._steps
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _find_cycles(self) -> List[str]:
        """DFS-based cycle detection. Returns list of nodes in cycle (empty if none)."""
        WHITE, GRAY, BLACK = 0, 1, 2
        color: Dict[str, int] = {sid: WHITE for sid in self._steps}
        cycle_nodes: List[str] = []

        def dfs(node: str) -> bool:
            color[node] = GRAY
            for succ in self._successors.get(node, set()):
                if color[succ] == GRAY:
                    cycle_nodes.append(succ)
                    return True
                if color[succ] == WHITE and dfs(succ):
                    return True
            color[node] = BLACK
            return False

        for node in self._steps:
            if color[node] == WHITE:
                if dfs(node):
                    break

        return cycle_nodes
