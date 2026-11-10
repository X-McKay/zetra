"""`platform/images.json` is the one list of platform images CI builds."""

from __future__ import annotations

import json
from pathlib import Path

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
