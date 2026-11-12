"""`platform/images.json` is the one list of platform images CI builds."""

from __future__ import annotations

import json
import posixpath
import re
from pathlib import Path

import pytest

PLATFORM = Path(__file__).parents[2]
REPO = PLATFORM.parent
IMAGES = json.loads((PLATFORM / "images.json").read_text())


def test_every_platform_dockerfile_has_an_entry():
    dockerfiles = {str(p.relative_to(REPO)) for p in PLATFORM.glob("*/Dockerfile")}
    assert dockerfiles, "no platform Dockerfile found"
    assert dockerfiles <= {image["dockerfile"] for image in IMAGES}


def test_every_entry_names_an_existing_dockerfile_and_context():
    for image in IMAGES:
        assert (REPO / image["dockerfile"]).is_file(), image
        assert (REPO / image["context"]).is_dir(), image


def test_repository_names_are_unique():
    names = [image["repository"] for image in IMAGES]
    assert len(names) == len(set(names)), names


def instructions(dockerfile: Path, keyword: str) -> list[list[str]]:
    """The arguments of each `keyword` instruction, flags such as --platform removed."""
    found = []
    for line in dockerfile.read_text().splitlines():
        words = line.split()
        if words and words[0].upper() == keyword:
            found.append([w for w in words[1:] if not w.startswith("--")])
    return found


def test_every_base_image_is_pinned_by_digest():
    for dockerfile in PLATFORM.glob("*/Dockerfile"):
        froms = instructions(dockerfile, "FROM")
        assert froms, dockerfile
        stages = set()
        for image, *alias in froms:
            if image not in stages:  # an earlier stage is pinned where it was defined
                assert re.fullmatch(r"[^@\s]+:[^@:\s]+@sha256:[0-9a-f]{64}", image), (
                    dockerfile,
                    image,
                )
            if alias:
                assert len(alias) == 2 and alias[0].upper() == "AS", (dockerfile, alias)
                stages.add(alias[1])


def test_build_arguments_are_declared_and_agree_with_dockerfile_defaults():
    for image in IMAGES:
        declared = dict(
            arg[0].partition("=")[::2] for arg in instructions(REPO / image["dockerfile"], "ARG")
        )
        for name, value in image["buildArgs"].items():
            assert name in declared, (image["repository"], name)
            # A default left in the Dockerfile is a second copy of the value CI passes.
            assert declared[name] in ("", value), (image["repository"], name, declared[name])


def copy_sources(text: str, where: str = "Dockerfile") -> list[str]:
    """Every source of each COPY and ADD that reads the build context (not `--from=` stages).

    It reads the plain forms only and fails on what it cannot follow, so that an unusual
    Dockerfile is looked at by a person: a bind mount of the context, a source built from a
    variable, the JSON form. A line continued with a backslash is joined first.
    """
    sources = []
    for line in re.sub(r"\\[ \t]*\n", " ", text).splitlines():
        words = line.split()
        if not words:
            continue
        keyword = words[0].upper()
        if keyword == "RUN":
            for mount in (w for w in words[1:] if w.startswith("--mount=")):
                options = mount.removeprefix("--mount=").split(",")
                # The type defaults to bind, and a bind mount without from= reads the context.
                kind = next((o.split("=", 1)[1] for o in options if o.startswith("type=")), "bind")
                from_stage = any(o.startswith("from=") for o in options)
                assert kind != "bind" or from_stage, f"{where}: a bind mount of the build context"
            continue
        if keyword not in ("COPY", "ADD"):
            continue
        if any(w.startswith("--from=") for w in words[1:]):
            continue
        args = [w for w in words[1:] if not w.startswith("--")]
        assert not args[0].startswith("["), f"{where}: JSON form COPY is not supported"
        local = [a for a in args[:-1] if "://" not in a]
        assert not any("$" in a for a in local), f"{where}: a COPY source built from a variable"
        sources += local
    return sources


def test_copy_sources_reads_the_plain_forms():
    text = (
        "FROM base\n"
        "COPY a b/c /dest/\n"
        "COPY --chmod=0555 d \\\n    e \\\n    /dest/\n"
        "COPY --from=build /out /out\n"
        "ADD https://example.invalid/x /x\n"
        "RUN --mount=type=cache,target=/root/.cache true\n"
        "RUN --mount=type=bind,from=build,source=/out,target=/in true\n"
    )
    assert copy_sources(text) == ["a", "b/c", "d", "e"]


@pytest.mark.parametrize(
    "line",
    [
        "RUN --mount=type=bind,source=controller/tests,target=/t true",
        "RUN --mount=source=controller/tests,target=/t true",
        "COPY contracts/${X} /dest/",
        "COPY $X /dest/",
        'COPY ["a", "/dest/"]',
    ],
)
def test_copy_sources_fails_on_what_it_cannot_follow(line):
    with pytest.raises(AssertionError):
        copy_sources(f"FROM base\n{line}\n")


def test_declared_inputs_cover_everything_the_dockerfile_copies():
    declared = [image for image in IMAGES if "inputs" in image]
    assert declared, "no entry declares inputs"
    for image in declared:
        sources = copy_sources((REPO / image["dockerfile"]).read_text(), image["dockerfile"])
        assert sources, image["repository"]
        for source in sources:
            path = posixpath.normpath(posixpath.join(image["context"], source))
            assert any(
                path == root or path.startswith(root.rstrip("/") + "/") for root in image["inputs"]
            ), (image["repository"], path, image["inputs"])


def test_declared_inputs_exist_and_are_not_the_whole_context():
    for image in IMAGES:
        for root in image.get("inputs", []):
            assert (REPO / root).exists(), (image["repository"], root)
            assert posixpath.normpath(root) != posixpath.normpath(image["context"])


def test_only_known_keys_in_images_json():
    allowed = {"repository", "dockerfile", "context", "inputs", "platform", "buildArgs"}
    for image in IMAGES:
        assert set(image) <= allowed, (image["repository"], set(image) - allowed)
