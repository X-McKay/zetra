#!/usr/bin/env python3
"""Validate generated standalone document structure, local links and offline assets."""

import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


class Document(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.links = []
        self.images = []
        self.headings = []
        self.svgs = 0
        self.code = 0
        self.text = []
        self.js_sources = []
        self.lang = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "id" in a:
            self.ids.append(a["id"])
        if tag == "html":
            self.lang = a.get("lang")
        if tag == "a":
            self.links.append(a.get("href", ""))
        if tag == "img":
            self.images.append(a)
        if tag == "svg":
            self.svgs += 1
            assert a.get("role") == "img" and a.get("aria-label"), (
                "SVG needs accessible image label"
            )
        if tag == "script" and a.get("src"):
            self.js_sources.append(a["src"])
        if tag == "h1":
            self.headings.append(tag)
        if tag == "pre":
            self.code += 1

    def handle_data(self, data):
        self.text.append(data)


def main():
    paths = [
        ROOT / "docs" / f"{n}.html"
        for n in (
            "index",
            "strategy",
            "developer",
            "deployment",
            "governance",
            "toolkit",
            "source-review",
            "integration-deep-dive",
            "use-case-walkthrough",
            "lifecycle-guide",
            "deployment-guide",
        )
    ]
    parsed = {}
    for p in paths:
        d = Document()
        source = p.read_text()
        d.feed(source)
        parsed[p] = d
        assert d.lang == "en", f"{p.name}: language missing"
        assert len(d.headings) == 1, f"{p.name}: expected single h1"
        assert len(d.ids) == len(set(d.ids)), f"{p.name}: duplicate IDs"
        assert not d.js_sources, f"{p.name}: document must be standalone/offline"
        assert "data-diagram=" not in source, f"{p.name}: diagram not rendered"
        assert not re.search(r"\[cite:|turn\d+(?:search|view)\d+", source), (
            f"{p.name}: unresolved source tokens"
        )
        assert not any("alt" not in im for im in d.images), f"{p.name}: missing image alternative"
        assert d.svgs >= 1, f"{p.name}: missing visual"
        if p.name == "lifecycle-guide.html":
            assert "zetra" not in source.lower(), "Developer guide must remain platform neutral"
            step_ids = [ident for ident in d.ids if re.fullmatch(r"dlc-step-\d+", ident)]
            assert step_ids == [f"dlc-step-{i}" for i in range(1, 10)], (
                "Developer guide must contain the nine steps in order"
            )
    for p, d in parsed.items():
        for link in d.links:
            u = urlsplit(link)
            if u.scheme or u.netloc:
                continue
            target = (p.parent / unquote(u.path)).resolve() if u.path else p
            assert target.exists(), f"{p.name}: broken local link {link}"
            if u.fragment:
                td = parsed.get(target)
                if td is None:
                    td = Document()
                    td.feed(target.read_text())
                assert unquote(u.fragment) in td.ids, f"{p.name}: missing anchor {link}"
    print(
        f"Verified {len(paths)} HTML documents: local links, unique IDs, accessible SVG, offline assets, single titles and resolved diagrams."
    )
    print(
        "Figures:",
        parsed[ROOT / "docs/index.html"].svgs,
        "Code blocks:",
        parsed[ROOT / "docs/index.html"].code,
    )


if __name__ == "__main__":
    main()
