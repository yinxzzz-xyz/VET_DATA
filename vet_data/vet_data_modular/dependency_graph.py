"""Deterministic dependency analysis for calculated signal definitions."""

from dataclasses import dataclass
from typing import Mapping, Sequence

from .calculated_signal import CalculatedSignalDefinition


@dataclass(frozen=True)
class DependencyGraphResult:
    order: tuple[str, ...]
    calculated_dependencies: Mapping[str, tuple[str, ...]]
    raw_dependencies: Mapping[str, tuple[str, ...]]
    cycle_nodes: tuple[str, ...]


class DependencyGraph:
    """Separate calculated nodes from raw inputs and sort calculated nodes."""

    def __init__(self, definitions: Sequence[CalculatedSignalDefinition]):
        self.definitions = {item.stable_id: item for item in definitions}
        if len(self.definitions) != len(definitions):
            raise ValueError("calculated signal stable_id 必须唯一")

    def analyze(self) -> DependencyGraphResult:
        ids = set(self.definitions)
        calculated = {
            key: tuple(dep for dep in item.dependencies if dep in ids)
            for key, item in self.definitions.items()
        }
        raw = {
            key: tuple(dep for dep in item.dependencies if dep not in ids)
            for key, item in self.definitions.items()
        }
        cycle_nodes = self._cycle_nodes(calculated)
        blocked = set(cycle_nodes)
        indegree = {
            key: sum(dep not in blocked for dep in dependencies)
            for key, dependencies in calculated.items() if key not in blocked
        }
        dependents = {key: [] for key in indegree}
        for key in indegree:
            for dependency in calculated[key]:
                if dependency in indegree:
                    dependents[dependency].append(key)
        ready = sorted(key for key, degree in indegree.items() if degree == 0)
        order = []
        while ready:
            key = ready.pop(0)
            order.append(key)
            for dependent in sorted(dependents[key]):
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    ready.append(dependent)
                    ready.sort()
        # Nodes downstream of a cycle are still deterministically reportable.
        remaining = sorted(set(indegree) - set(order))
        order.extend(remaining)
        return DependencyGraphResult(
            tuple(order), calculated, raw, tuple(sorted(cycle_nodes))
        )

    @staticmethod
    def _cycle_nodes(edges: Mapping[str, tuple[str, ...]]) -> set[str]:
        index = 0
        indices = {}
        lowlinks = {}
        stack = []
        on_stack = set()
        cycles = set()

        def visit(node):
            nonlocal index
            indices[node] = lowlinks[node] = index
            index += 1
            stack.append(node)
            on_stack.add(node)
            for dependency in sorted(edges[node]):
                if dependency not in indices:
                    visit(dependency)
                    lowlinks[node] = min(lowlinks[node], lowlinks[dependency])
                elif dependency in on_stack:
                    lowlinks[node] = min(lowlinks[node], indices[dependency])
            if lowlinks[node] == indices[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.remove(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1 or node in edges[node]:
                    cycles.update(component)

        for node in sorted(edges):
            if node not in indices:
                visit(node)
        return cycles