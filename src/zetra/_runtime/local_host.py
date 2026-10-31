"""Linux trusted-host lifecycle; leases fence admission, never select progress."""

import asyncio
import fcntl
import json
import os
import shutil
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from ..local import LocalModelConfig
from ..manifest import AgentSpec, digest, load_agent, source_digest
from .artifacts import Position
from .process import run_child
from .records import canonical_bytes


def safe_path(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("local state paths must be absolute")
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("local paths cannot contain symlinks")
    return path


def private_directory(path):
    path = safe_path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    stat = path.stat()
    if not path.is_dir() or stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise ValueError("local state directory must be private and caller-owned")
    return path


def read_json(path):
    path = safe_path(path)
    if path.stat().st_size > 262144:
        raise ValueError("local metadata exceeds limit")
    return json.loads(path.read_bytes())


def sync_directory(path):
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def write_json(path, value):
    """Publish lifecycle metadata before acknowledging it, including the directory."""
    path = safe_path(path)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_settings(settings):
    if not isinstance(settings, dict) or set(settings) != {
        "project_root",
        "source_sha256",
        "agent",
        "model_config",
    }:
        raise ValueError("invalid local execution settings")
    project = safe_path(settings["project_root"])
    if not project.is_dir():
        raise ValueError("local project does not exist")
    source = settings["source_sha256"]
    if (
        not isinstance(source, str)
        or len(source) != 64
        or any(item not in "0123456789abcdef" for item in source)
    ):
        raise ValueError("invalid source identity")
    spec = AgentSpec.model_validate(settings["agent"]).validate_policy()
    config = settings["model_config"]
    if config is not None:
        config = LocalModelConfig.model_validate(config).model_dump()
    elif spec.model != "fixture":
        raise ValueError("local inference requires explicit model configuration")
    return {
        "project_root": str(project),
        "source_sha256": source,
        "agent": spec.model_dump(),
        "model_config": config,
    }


def verify_inputs(settings):
    root = safe_path(settings["project_root"])
    if (
        source_digest(root) != settings["source_sha256"]
        or load_agent(root, settings["agent"]["name"]).model_dump() != settings["agent"]
    ):
        raise ValueError("local project inputs changed")


class HostSession:
    """One exact execution, cold child per boundary, retained admission fences."""

    def __init__(self, root, objects_root, *, settings, session, message):
        if sys.platform != "linux":
            raise ValueError("local Temporal requires Linux")
        self.settings = validate_settings(settings)
        self.manifest = digest(self.settings)
        if not isinstance(session, str) or not session or len(session) > 128:
            raise ValueError("invalid local execution identity")
        if not isinstance(message, str) or not 0 < len(message.encode()) <= 65536:
            raise ValueError("invalid local input")
        self.session, self.message = session, message
        self.root = private_directory(root)
        self.objects_root = private_directory(objects_root)
        project = Path(self.settings["project_root"])
        if self.root == project or project in self.root.parents or self.root in project.parents:
            raise ValueError("local state cannot overlap project source")
        if self.objects_root == self.root or self.root in self.objects_root.parents:
            raise ValueError("artifact backing cannot be disposable execution state")
        self.creations = set()

    @property
    def identity(self):
        return {
            "version": 1,
            "session": self.session,
            "settings_sha256": self.manifest,
            "message_sha256": digest(self.message),
        }

    @asynccontextmanager
    async def lease(self):
        private_directory(self.root)
        fd = os.open(self.root / "lease", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            async with asyncio.timeout(38):
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        await asyncio.sleep(0.05)
            yield
        finally:
            os.close(fd)

    def state(self):
        if (self.root / "closed").exists():
            raise ValueError("local execution is closed")
        path = self.root / "state.json"
        if not path.exists():
            return None
        value = read_json(path)
        if (
            not isinstance(value, dict)
            or any(value.get(key) != expected for key, expected in self.identity.items())
            or type(value.get("generation")) is not int
            or not 1 <= value["generation"] <= 64
        ):
            raise ValueError("local execution metadata mismatch")
        Position.model_validate(value["position"])
        return value

    def binding(self, state):
        return {
            "session": self.session,
            "generation": state["generation"],
            "position": state["position"],
        }

    def check_binding(self, binding, state):
        if (
            not isinstance(binding, dict)
            or type(binding.get("generation")) is not int
            or state is None
            or binding != self.binding(state)
        ):
            raise ValueError("stale or wrong local binding")

    async def provision(self, start):
        position = Position.model_validate(start["position"]).model_dump()
        async with self.lease():
            current = self.state()
            if current is None:
                current = {**self.identity, "generation": 1, "position": position}
                write_json(self.root / "state.json", current)
            if current["generation"] != 1 or current["position"] != position:
                raise ValueError("local generation already retired")
            return self.binding(current)

    async def replace(self, command):
        old = command["binding"]
        position = Position.model_validate(command["position"]).model_dump()
        async with self.lease():
            current = self.state()
            if (
                not isinstance(old, dict)
                or old.get("session") != self.session
                or type(old.get("generation")) is not int
                or current is None
            ):
                raise ValueError("invalid replacement binding")
            generation = old["generation"] + 1
            if current["generation"] == generation and current["position"] == position:
                return self.binding(current)
            self.check_binding(old, current)
            if generation > 64:
                raise ValueError("local generation limit")
            current = {**self.identity, "generation": generation, "position": position}
            write_json(self.root / "state.json", current)
            return self.binding(current)

    async def wait_ready(self, binding):
        async with self.lease():
            self.check_binding(binding, self.state())
        return binding

    async def call(self, action, command):
        if action not in ("advance", "execute"):
            raise ValueError("unsupported local action")
        result = await run_child(
            "zetra.testing.local_temporal",
            {
                "root": str(self.root),
                "objects_root": str(self.objects_root),
                "settings": self.settings,
                "session": self.session,
                "message": self.message,
                "parent_pid": os.getpid(),
                "action": action,
                "command": command,
            },
            timeout=38,
            max_output=1048576,
        )
        # Child interpreter finalizers run after its coroutine has returned.
        # Read metadata only, after confirmed process exit, before accepting bytes.
        await asyncio.to_thread(verify_inputs, self.settings)
        return result

    async def close(self, command):
        try:
            async with self.lease():
                # Do not require acknowledged provisioning: fence late startup too.
                if (self.root / "closed").exists():
                    if read_json(self.root / "closed") != self.identity:
                        raise ValueError("local cleanup identity mismatch")
                else:
                    self.state()
                    write_json(self.root / "closed", self.identity)
                work = safe_path(self.root / "work")
                if work.exists():
                    shutil.rmtree(work)
                if work.exists():
                    raise OSError("local workspace removal unconfirmed")
                return {"cleanup": "DELETED"}
        except (OSError, ValueError, TimeoutError):
            return {"cleanup": "NOT_CONFIRMED"}
