from pathlib import Path

from zetra._runtime.runtime import Runtime as BaseRuntime
from zetra.testing.reporter import Definition


class Runtime(BaseRuntime):
    def __init__(self, root, **kwargs):
        super().__init__(
            root,
            definition=Definition(Path(root) / "workspace", kwargs.get("provider", "scripted")),
            **kwargs,
        )
