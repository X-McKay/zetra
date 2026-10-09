"""Offline deterministic retrieval: user strings never become tool commands."""

from dataclasses import dataclass

from zetra.runtime import RuntimeDependencies


@dataclass
class KnowledgeAgent:
    dependencies: RuntimeDependencies

    def run(self, request: dict) -> dict:
        if (
            not isinstance(request, dict)
            or set(request) != {"key"}
            or not isinstance(request["key"], str)
            or len(request["key"]) > 128
        ):
            raise ValueError("Request must contain one string key <= 128 characters")
        return {
            "answer": self.dependencies.read_knowledge(request["key"]),
            "source": "approved-knowledge-fixture",
        }


def create_agent(dependencies: RuntimeDependencies) -> KnowledgeAgent:
    return KnowledgeAgent(dependencies)
