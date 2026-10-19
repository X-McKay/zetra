"""Build from one captured package; bind source labels to an immutable image."""

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import tomllib
from importlib.resources import files
from pathlib import Path

from .manifest import Release, digest, load_agent, project_file, source_digest, source_files


def snapshot_package(root, target):
    before = source_digest(root)
    root, target = Path(root).resolve(), Path(target)
    target.mkdir(parents=True, exist_ok=False)
    for path in source_files(root):
        output = target / path.relative_to(root)
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, output)
    if before != source_digest(root) or before != source_digest(target):
        raise ValueError("source changed during build snapshot")
    return before


def sdk_dependency(config):
    """Read the portable package dependency, also retained in wheel metadata."""
    dependencies = config.get("project", {}).get("dependencies", [])
    refs = [
        re.fullmatch(r"zetra\s*@\s*git\+(https://[^@\s?#]+)@([a-f0-9]{40})", item)
        for item in dependencies
        if isinstance(item, str)
    ]
    refs = [match for match in refs if match is not None]
    if len(refs) != 1 or config.get("tool", {}).get("uv", {}).get("sources", {}).get("zetra"):
        raise ValueError(
            "zetra must be a single standard HTTPS Git dependency pinned to a full commit"
        )
    return refs[0].groups()


def lock_identity(root):
    """Require the SDK's exact Git revision and reject local dependency overlays."""
    raw = project_file(root, "uv.lock").read_bytes()
    lock = tomllib.loads(raw.decode())
    config = tomllib.loads(project_file(root, "pyproject.toml").read_text())
    url, revision = sdk_dependency(config)
    packages = lock.get("package", [])
    sdk = [item for item in packages if item.get("name") == "zetra"]
    expected = url + "?rev=" + revision + "#" + revision
    if len(sdk) != 1 or sdk[0].get("source", {}).get("git") != expected:
        raise ValueError("lockfile does not match the pinned SDK revision")
    for item in packages:
        source = item.get("source", {})
        for key in ("path", "directory", "editable", "virtual"):
            if key in source and (source[key] != "." or item is sdk[0]):
                raise ValueError("local dependency overrides are not supported")
    return {"lock_sha256": hashlib.sha256(raw).hexdigest(), "sdk_revision": revision}


def make_release(spec, source, image, locked):
    from ._runtime.openshell import policy, vllm_policy

    selected = vllm_policy() if spec.policy == "vllm-chat" else policy()
    return Release(
        **locked,
        agent=spec,
        image=image,
        source_sha256=source,
        policy_sha256=hashlib.sha256(selected.SerializeToString(deterministic=True)).hexdigest(),
    )


def build(root, *, tag, kind=False, agent=None):
    if not re.fullmatch(r"[a-z][a-z0-9_-]*:[a-z0-9_.-]+", tag):
        raise ValueError("local image tag must be name:tag")
    root = Path(root).resolve()
    with tempfile.TemporaryDirectory() as temp:
        context = Path(temp)
        source = snapshot_package(root, context / "agent")
        spec = load_agent(context / "agent", agent)
        locked = lock_identity(context / "agent")
        recipe = files("zetra").joinpath("agent.Dockerfile").read_text()
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
        return make_release(spec, source, image, locked).model_dump()
    digests = info.get("RepoDigests") or []
    if digests:
        return make_release(spec, source, digests[0], locked).model_dump()
    return {
        **locked,
        "status": "BUILT_LOCAL",
        "tag": tag,
        "image_id": info["Id"],
        "source_sha256": source,
        "next": "Use --kind to load a local immutable release; external registry publication is explicit.",
    }


def release_for(root, image, *, agent=None):
    Release.immutable_image(image)
    spec = load_agent(root, agent)
    locked = lock_identity(root)
    source = source_digest(root)
    info = json.loads(subprocess.check_output(["docker", "image", "inspect", image]))[0]
    labels = info.get("Config", {}).get("Labels") or {}
    if labels.get("zetra.source-sha256") != source or labels.get("zetra.agent-sha256") != digest(
        spec.model_dump()
    ):
        raise ValueError("image build labels do not match source and agent configuration")
    return make_release(spec, source, image, locked)
