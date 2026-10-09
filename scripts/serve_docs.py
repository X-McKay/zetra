"""Loopback preview of Git-tracked public files; never expose local tooling data."""

from __future__ import annotations

import argparse
import subprocess
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def main(port: int) -> None:
    if not 1 <= port <= 65535:
        raise ValueError("Port must be 1..65535")
    tracked = (
        subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True, timeout=10
        )
        .stdout.decode()
        .split("\0")
    )
    public = {
        name for name in tracked if name and not any(p.startswith(".") for p in Path(name).parts)
    }

    class Handler(SimpleHTTPRequestHandler):
        def public_path(self) -> Path | None:
            path = unquote(urlsplit(self.path).path)
            if path in {"/", "/docs", "/docs/"}:
                path = "/docs/index.html"
            if "\0" in path:
                return None
            candidate = ROOT / path.lstrip("/")
            resolved = candidate.resolve()
            if resolved != candidate or not resolved.is_relative_to(ROOT):
                return None
            relative = resolved.relative_to(ROOT).as_posix()
            return resolved if relative in public and resolved.is_file() else None

        def translate_path(self, path: str) -> str:
            allowed = self.public_path()
            if allowed is None:
                raise ValueError("Unvalidated public path")
            return str(allowed)

        def do_GET(self) -> None:
            if urlsplit(self.path).path in {"/", "/docs"}:
                self.send_response(302)
                self.send_header("Location", "/docs/")
                self.end_headers()
            elif self.public_path() is None:
                self.send_error(404, "No public repository file")
            else:
                super().do_GET()

        def do_HEAD(self) -> None:
            if self.public_path() is None:
                self.send_error(404, "No public repository file")
            else:
                super().do_HEAD()

    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        print(f"Zetra preview: http://127.0.0.1:{port}/docs/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    main(parser.parse_args().port)
