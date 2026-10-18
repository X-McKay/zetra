"""Build all qualification images from this checkout into the dedicated kind cluster."""

import json
import shutil
import subprocess
from pathlib import Path

from zetra.build import build

root = Path(__file__).resolve().parents[1]
state = root / ".state"
state.mkdir(exist_ok=True)


def load(tag, dockerfile="Dockerfile"):
    subprocess.run(["docker", "build", "-f", dockerfile, "-t", tag, "."], cwd=root, check=True)
    subprocess.run(["kind", "load", "docker-image", tag, "--name", "zetra-validation"], check=True)


load("zetra-validation:local")
recipe = (root / "Dockerfile").read_text().replace(" --compile-bytecode", "")
recipe = recipe.replace(
    "RUN /app/.venv/bin/python -m compileall -q /usr/local/lib/python3.12\n", ""
)
(state / "Dockerfile.source").write_text(recipe)
load("zetra-validation:source", str(state / "Dockerfile.source"))

for name, package in [
    ("reporter", "workspace_reporter"),
    ("reader", "document_reader"),
    ("live", "workspace_reporter"),
    ("blocked", "workspace_reporter"),
]:
    path = root / "examples" / package
    if name in ("live", "blocked"):
        path = state / (name + "-agent")
        if path.exists():
            shutil.rmtree(path)
        shutil.copytree(
            root / "examples" / package,
            path,
            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"),
        )
        if name == "live":
            manifest = path / "agent.yaml"
            manifest.write_text(
                manifest.read_text()
                .replace("model: fixture", "model: vllm")
                .replace("policy: workspace", "policy: vllm-chat")
            )
        else:
            tools = path / "src/workspace_reporter/tools.py"
            tools.write_text(
                tools.read_text()
                .replace("def tools_for(workspace):", "import asyncio\n\ndef tools_for(workspace):")
                .replace(
                    '        return {"files":',
                    '        await asyncio.Event().wait()\n        return {"files":',
                )
            )
    release = build(path, tag="zetra-" + name + ":local", kind=True)
    (state / (name + "-release.json")).write_text(json.dumps(release, indent=2) + "\n")

from zetra.scaffold import scaffold

generated = state / "generated-agent"
if generated.exists():
    shutil.rmtree(generated)
scaffold(generated, "generated_agent")
release = build(generated, tag="zetra-generated:local", kind=True)
(state / "generated-release.json").write_text(json.dumps(release, indent=2) + "\n")
