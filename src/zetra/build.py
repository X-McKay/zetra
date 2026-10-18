"""Build from one captured package; bind source labels to an immutable image."""

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from importlib.resources import files
from pathlib import Path

from .manifest import Release, digest, load_agent, source_digest


def snapshot_package(root, target):
    before = source_digest(root)
    shutil.copytree(
        root,
        target,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", ".zetra", "__pycache__", ".pytest_cache", "dist", "build", "*.egg-info"
        ),
    )
    if before != source_digest(root) or before != source_digest(target):
        raise ValueError("source changed during build snapshot")
    return before


def make_release(spec, source, image):
    from ._runtime.openshell import policy, vllm_policy

    selected = vllm_policy() if spec.policy == "vllm-chat" else policy()
    return Release(
        agent=spec,
        image=image,
        source_sha256=source,
        policy_sha256=hashlib.sha256(selected.SerializeToString(deterministic=True)).hexdigest(),
    )


def build(root, *, tag, kind=False):
    if not re.fullmatch(r"[a-z][a-z0-9_-]*:[a-z0-9_.-]+", tag):
        raise ValueError("local image tag must be name:tag")
    root = Path(root).resolve()
    sdk = Path(str(files("zetra")))
    assets = sdk / "_build"
    if not assets.is_dir():
        assets = sdk.parents[1]
    with tempfile.TemporaryDirectory() as temp:
        context = Path(temp)
        source = snapshot_package(root, context / "agent")
        spec = load_agent(context / "agent")
        for name in ("pyproject.toml", "uv.lock", "Dockerfile"):
            shutil.copy2(assets / name, context / name)
        shutil.copytree(
            sdk, context / "src/zetra", ignore=shutil.ignore_patterns("__pycache__", "_build")
        )
        recipe = (context / "Dockerfile").read_text()
        recipe = recipe.replace(
            "WORKDIR /sandbox",
            "COPY agent /app/agent\n"
            "RUN uv export --frozen --no-dev --no-emit-project --format requirements-txt > /tmp/constraints.txt "
            "&& uv pip install --python /app/.venv/bin/python --constraint /tmp/constraints.txt /app/agent "
            "&& /app/.venv/bin/python -m compileall -q /app\nWORKDIR /sandbox",
        )
        recipe += f'\nLABEL zetra.source-sha256="{source}" zetra.agent-sha256="{digest(spec.model_dump())}"\n'
        (context / "Dockerfile.agent").write_text(recipe)
        subprocess.run(
            ["docker", "build", "-f", str(context / "Dockerfile.agent"), "-t", tag, str(context)],
            check=True,
            stdout=__import__("sys").stderr,
        )
        if source != source_digest(root):
            raise ValueError("source changed during image build; release withheld")
    info = json.loads(subprocess.check_output(["docker", "image", "inspect", tag]))[0]
    if kind:
        node = "zetra-validation-control-plane"
        subprocess.run(
            ["kind", "load", "docker-image", tag, "--name", "zetra-validation"], check=True
        )
        rows = subprocess.check_output(
            ["docker", "exec", node, "ctr", "-n", "k8s.io", "images", "list"], text=True
        ).splitlines()
        name = "docker.io/library/" + tag
        matches = [row.split()[2] for row in rows if row.split() and row.split()[0] == name]
        if len(matches) != 1 or not re.fullmatch(r"sha256:[a-f0-9]{64}", matches[0]):
            raise ValueError("loaded image digest missing")
        image = "docker.io/library/" + tag.split(":")[0] + "@" + matches[0]
        subprocess.run(
            [
                "docker",
                "exec",
                node,
                "ctr",
                "-n",
                "k8s.io",
                "images",
                "tag",
                "--force",
                name,
                image,
            ],
            check=True,
        )
        return make_release(spec, source, image).model_dump()
    digests = info.get("RepoDigests") or []
    if digests:
        return make_release(spec, source, digests[0]).model_dump()
    return {
        "status": "BUILT_LOCAL",
        "tag": tag,
        "image_id": info["Id"],
        "source_sha256": source,
        "next": "Use --kind to load a local immutable release; external registry publication is explicit.",
    }


def release_for(root, image):
    Release.immutable_image(image)
    spec = load_agent(root)
    source = source_digest(root)
    info = json.loads(subprocess.check_output(["docker", "image", "inspect", image]))[0]
    labels = info.get("Config", {}).get("Labels") or {}
    if labels.get("zetra.source-sha256") != source or labels.get("zetra.agent-sha256") != digest(
        spec.model_dump()
    ):
        raise ValueError("image build labels do not match source and agent configuration")
    return make_release(spec, source, image)
