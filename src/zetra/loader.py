"""Import only explicitly trusted code at run time; check/eval-static never import."""

import importlib
import sys
from pathlib import Path

from .manifest import ContractError, load
from .runtime import RuntimeDependencies


def construct(root: Path, dependencies: RuntimeDependencies):
    manifest = load(root)
    module_name, factory_name = manifest.entrypoint.split(":")
    source_root = (root / "src").resolve()
    expected = source_root.joinpath(*module_name.split(".")).with_suffix(".py")
    expected_package = source_root.joinpath(*module_name.split("."), "__init__.py")
    expected = expected if expected.is_file() else expected_package
    if not expected.is_file() or not expected.resolve().is_relative_to(source_root):
        raise ContractError("Entrypoint module must be inside the agent src directory")
    sys.path.insert(0, str(source_root))
    try:
        module = importlib.import_module(module_name)
        origin = module.__file__
        if not isinstance(origin, str) or Path(origin).resolve() != expected.resolve():
            raise ContractError(
                "Entrypoint resolves to a different loaded module; run agents in separate processes"
            )
        factory = getattr(module, factory_name, None)
        if not callable(factory):
            raise ContractError("Entrypoint factory is not callable")
        agent = factory(dependencies)
        if not callable(getattr(agent, "run", None)):
            raise ContractError("Factory must return an object with run(request: dict) -> dict")
        return agent
    finally:
        sys.path.pop(0)
