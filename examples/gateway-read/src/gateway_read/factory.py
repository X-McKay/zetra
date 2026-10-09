from dataclasses import dataclass

from zetra.runtime import Denied, RuntimeDependencies


@dataclass
class GatewayReadAgent:
    dependencies: RuntimeDependencies

    def run(self, request: object) -> dict:
        if not isinstance(request, dict) or request:
            raise Denied("Status request accepts exactly an empty object")
        return {"status": self.dependencies.read_status()}


def create_agent(dependencies: RuntimeDependencies) -> GatewayReadAgent:
    return GatewayReadAgent(dependencies)
