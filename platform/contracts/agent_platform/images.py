"""Which platform images a push to `main` releases, and the build facts of one image.

`platform/images.json` is the one list of platform images. This module reads it (the same
file `platform.yml` and `tests/test_images.py` read) and decides, from the two commits of a
push, which entries have changed build inputs. The release workflow runs it as a plain script,
before any dependency is installed, so it uses only the standard library:

    python3 -I images.py changed BEFORE AFTER   # JSON array of the entries to release
    python3 -I images.py entry NAME             # one entry as NUL-separated fields
    python3 -I images.py metadata-digest FILE   # digest from `buildx --metadata-file`
    python3 -I images.py registry-auth FILE HOST

An image is released when the push changed its Dockerfile, a file under its build context, or
its own entry in images.json. An entry may carry `inputs`, a list of repository paths; the
build context is then not used for change detection and a file under one of the `inputs` counts
instead (a context such as `platform` is much wider than what its Dockerfile copies). A test
holds every `inputs` list to cover the sources its Dockerfile copies, as far as the test can
read them. The two ignore files BuildKit consults (`<Dockerfile>.dockerignore` and
`<context>/.dockerignore`) always count. `platform/images.json` is never a build input of its own
accord: no Dockerfile copies it, so it counts only through the entry comparison.

Every field of an entry reaches a `docker buildx build` command line on a runner that holds a
registry login, so `parse_images` refuses anything but paths inside the repository, the two
platforms the cluster runs and plain build arguments.
"""

from __future__ import annotations

import argparse
import json
import posixpath
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

IMAGES_FILE = "platform/images.json"
ZEROS = re.compile(r"0+")
NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
ARG_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
PLATFORMS = ("linux/amd64", "linux/arm64")
FIELDS = ("repository", "dockerfile", "context", "platform", "buildArgs")


class ImagesError(Exception):
    """images.json or a commit could not be read; the caller must stop, not guess."""


def _printable(value: str) -> bool:
    return not any(ord(c) < 32 or ord(c) == 127 for c in value)


def repo_path(value: Any) -> bool:
    """A path inside the repository as written: relative, no `..`, nothing a command reads as an option."""
    return (
        isinstance(value, str)
        and value != ""
        and not value.startswith(("/", "-"))
        and ".." not in value.split("/")
        and _printable(value)
    )


def build_args_ok(value: Any) -> bool:
    return isinstance(value, dict) and all(
        isinstance(k, str) and ARG_NAME.fullmatch(k) and isinstance(v, str) and _printable(v)
        for k, v in value.items()
    )


def parse_images(text: str, source: str = IMAGES_FILE) -> list[dict[str, Any]]:
    """The entries of an images.json document, checked for the fields the builds read."""
    try:
        images = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ImagesError(f"{source}: not valid JSON: {exc}") from exc
    if not isinstance(images, list):
        raise ImagesError(f"{source}: expected a list of images")
    seen: set[str] = set()
    for image in images:
        if not isinstance(image, dict) or any(key not in image for key in FIELDS):
            raise ImagesError(f"{source}: every entry needs {', '.join(FIELDS)}: {image!r}")
        name = image["repository"]
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ImagesError(f"{source}: bad repository name {name!r}")
        if name in seen:
            raise ImagesError(f"{source}: repository {name!r} listed twice")
        seen.add(name)
        for key in ("dockerfile", "context"):
            if not repo_path(image[key]):
                raise ImagesError(
                    f"{source}: {name}: {key} must be a path inside the repository, "
                    f"not {image[key]!r}"
                )
        if image["platform"] not in PLATFORMS:
            raise ImagesError(
                f"{source}: {name}: platform must be one of {', '.join(PLATFORMS)}, "
                f"not {image['platform']!r}"
            )
        if not build_args_ok(image["buildArgs"]):
            raise ImagesError(
                f"{source}: {name}: buildArgs must map argument names to single-line strings"
            )
        inputs = image.get("inputs")
        if inputs is not None and (
            not isinstance(inputs, list) or not inputs or not all(repo_path(p) for p in inputs)
        ):
            raise ImagesError(
                f"{source}: {name}: inputs must be a non-empty list of paths inside the repository"
            )
    return images


def load_images(repo: Path) -> list[dict[str, Any]]:
    return parse_images((repo / IMAGES_FILE).read_text())


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if check and result.returncode != 0:
        raise ImagesError(f"git {' '.join(args)}: {result.stderr.decode(errors='replace').strip()}")
    return result


def commit_exists(repo: Path, rev: str) -> bool:
    return _git(repo, "cat-file", "-e", f"{rev}^{{commit}}", check=False).returncode == 0


def images_at(repo: Path, rev: str) -> list[dict[str, Any]]:
    """The entries at a commit; a commit without the file has no images."""
    result = _git(repo, "show", f"{rev}:{IMAGES_FILE}", check=False)
    if result.returncode != 0:
        if _git(repo, "cat-file", "-e", f"{rev}:{IMAGES_FILE}", check=False).returncode != 0:
            return []
        raise ImagesError(f"cannot read {IMAGES_FILE} at {rev}")
    try:
        text = result.stdout.decode()
    except UnicodeDecodeError as exc:
        raise ImagesError(f"{IMAGES_FILE} at {rev} is not UTF-8") from exc
    return parse_images(text, f"{IMAGES_FILE} at {rev}")


def changed_paths(repo: Path, before: str, after: str) -> list[str]:
    """Every path whose content differs between the two trees.

    A tree comparison, not a walk of the commits between them, so a merge commit, a squash
    and a push of several commits all give the push's net effect. Renames are reported as a
    deletion and an addition so that the old location counts as changed too. A file name
    that is not UTF-8 is kept (its odd bytes escaped), so it still counts under its directory.
    """
    out = _git(repo, "diff", "--name-only", "--no-renames", "-z", before, after).stdout
    return [p.decode(errors="surrogateescape") for p in out.split(b"\0") if p]


def under(path: str, directory: str) -> bool:
    directory = posixpath.normpath(directory)
    return directory == "." or path == directory or path.startswith(directory + "/")


def inputs_changed(image: dict[str, Any], paths: list[str]) -> bool:
    """The Dockerfile and its ignore files, plus the declared `inputs` or, without them, the
    whole build context."""
    dockerfile = posixpath.normpath(image["dockerfile"])
    # BuildKit reads <Dockerfile>.dockerignore if it exists, else .dockerignore in the context.
    always = {
        dockerfile,
        dockerfile + ".dockerignore",
        posixpath.normpath(posixpath.join(image["context"], ".dockerignore")),
    }
    roots = image.get("inputs") or [image["context"]]
    return any(
        p in always or (p != IMAGES_FILE and any(under(p, root) for root in roots)) for p in paths
    )


@dataclass
class Selection:
    images: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def select(
    before_images: list[dict[str, Any]],
    after_images: list[dict[str, Any]],
    paths: list[str],
) -> Selection:
    """The entries of `after_images` to release, in file order."""
    before_by_name = {image["repository"]: image for image in before_images}
    after_names = {image["repository"] for image in after_images}
    selection = Selection()
    for image in after_images:
        name = image["repository"]
        old = before_by_name.get(name)
        if old is None:
            why = "new entry in images.json"
        elif old != image:
            why = "entry edited in images.json"
        elif inputs_changed(image, paths):
            why = "Dockerfile or build context changed"
        else:
            continue
        selection.images.append(image)
        selection.notes.append(f"release {name}: {why}")
    for name in sorted(set(before_by_name) - after_names):
        selection.notes.append(f"{name}: removed from images.json, nothing to build")
    return selection


def changed(repo: Path, before: str, after: str) -> Selection:
    if not commit_exists(repo, after):
        raise ImagesError(f"the pushed commit {after} is not in the checkout")
    # A first push of a branch has an all-zero "before"; a forced or truncated history can
    # leave one this checkout never fetched. Neither says what changed, so nothing is built:
    # building everything would push eight images nobody asked for, and the builds are not
    # reproducible, so the next real change could not tell them apart. A later push that
    # changes an input releases that image as usual.
    if ZEROS.fullmatch(before) or not commit_exists(repo, before):
        why = "all zeros" if ZEROS.fullmatch(before) else "not in this checkout"
        return Selection(notes=[f"releasing nothing: the before commit {before} is {why}"])
    selection = select(
        images_at(repo, before), images_at(repo, after), changed_paths(repo, before, after)
    )
    if not selection.images:
        selection.notes.append("no image has changed build inputs")
    return selection


def entry_fields(repo: Path, name: str) -> list[str]:
    """dockerfile, context, platform, then one KEY=VALUE per build argument."""
    for image in load_images(repo):
        if image["repository"] == name:
            args = [f"{key}={value}" for key, value in image["buildArgs"].items()]
            return [image["dockerfile"], image["context"], image["platform"], *args]
    raise ImagesError(f"{name!r} is not an entry in {IMAGES_FILE}")


def metadata_digest(text: str) -> str:
    """The pushed manifest digest from `docker buildx build --metadata-file`."""
    try:
        digest = json.loads(text)["containerimage.digest"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ImagesError("build metadata has no containerimage.digest") from exc
    if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
        raise ImagesError("build metadata has a malformed digest")
    return digest


def registry_auth_present(text: str, host: str) -> bool:
    """Whether a docker client configuration holds credentials for `host`; never echoes any."""
    try:
        config = json.loads(text)
    except json.JSONDecodeError:
        return False
    if not isinstance(config, dict):
        return False
    helpers = config.get("credHelpers")
    if isinstance(helpers, dict) and helpers.get(host):
        return True
    auths = config.get("auths")
    if not isinstance(auths, dict):
        return False
    for key, value in auths.items():
        bare = re.sub(r"^https?://", "", key).split("/")[0]
        if bare == host and isinstance(value, dict) and value.get("auth"):
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("changed", help="JSON array of the images a push releases")
    cmd.add_argument("before")
    cmd.add_argument("after")
    cmd.add_argument("--repo", default=".")
    cmd = sub.add_parser("entry", help="one image's build facts, NUL separated")
    cmd.add_argument("name")
    cmd.add_argument("--repo", default=".")
    cmd = sub.add_parser("metadata-digest")
    cmd.add_argument("file")
    cmd = sub.add_parser("registry-auth")
    cmd.add_argument("file")
    cmd.add_argument("host")
    args = parser.parse_args(argv)
    try:
        if args.command == "changed":
            selection = changed(Path(args.repo), args.before, args.after)
            for note in selection.notes:
                print(note, file=sys.stderr)
            print(json.dumps(selection.images, separators=(",", ":")))
        elif args.command == "entry":
            sys.stdout.write("".join(f + "\0" for f in entry_fields(Path(args.repo), args.name)))
        elif args.command == "metadata-digest":
            print(metadata_digest(Path(args.file).read_text()))
        else:
            try:
                text = Path(args.file).read_text()
            except OSError:
                return 1
            return 0 if registry_auth_present(text, args.host) else 1
    except ImagesError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
