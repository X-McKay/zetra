"""Verify built wheel resources against their canonical source bytes."""

import argparse
from pathlib import Path
from zipfile import ZipFile


def check(wheel: Path) -> None:
    source = Path(__file__).resolve().parents[2] / "src"
    resources = (
        Path("agent_package/agents/document_reader/agent.yaml"),
        Path("agent_package/agents/document_reader/instructions/base.md"),
    )
    with ZipFile(wheel) as archive:
        for resource in resources:
            try:
                actual = archive.read(resource.as_posix())
            except KeyError as exc:
                raise ValueError(f"wheel is missing {resource}") from exc
            if actual != (source / resource).read_bytes():
                raise ValueError(f"wheel resource differs from source: {resource}")
    print(f"Verified packaged agent manifest and instructions: {wheel.name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheels", type=Path, nargs="+")
    args = parser.parse_args()
    for wheel in args.wheels:
        check(wheel)


if __name__ == "__main__":
    main()
