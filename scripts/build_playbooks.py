#!/usr/bin/env python3
"""Build portable HTML playbooks from reviewed chapter fragments and local assets."""

import hashlib
import re
from html import escape
from pathlib import Path

from lifecycle_diagrams import lifecycle_diagram
from technical_diagrams import technical_diagram

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
CSS = (DOCS / "assets/playbook.css").read_text()
JS = (DOCS / "assets/playbook.js").read_text()
GUIDE_CSS = (DOCS / "assets/developer-guide.css").read_text()
GUIDE_JS = (DOCS / "assets/developer-guide.js").read_text()
CHAPTERS = [
    ("strategy", "Agent Strategy", "Purpose, outcomes and the platform operating model"),
    (
        "developer",
        "Agent Developer Playbook",
        "Contracts, evaluation and a repeatable development lifecycle",
    ),
    (
        "deployment",
        "Agent Deployment Playbook",
        "Compile, qualify and operate the enforcement stack",
    ),
    (
        "governance",
        "Risk, Governance & Oversight",
        "Risk decisions, catalog evidence and operational control",
    ),
    ("toolkit", "Zetra Toolkit", "Working commands, examples and implementation boundaries"),
    ("source-review", "Source Review", "Provenance, corrections and upstream references"),
    (
        "integration-deep-dive",
        "Integration Deep Dive",
        "Implementation contracts and qualification for technical leaders",
    ),
    (
        "use-case-walkthrough",
        "Incident Triage Walkthrough",
        "One incident from typed package to approved ticket and revocation",
    ),
    (
        "lifecycle-guide",
        "Agent Developer Guide",
        "A practical playbook for building, testing, improving and operating an agent",
    ),
]
COLORS = {
    "teal": ("#e3f5f0", "#087f85"),
    "blue": ("#e6eef9", "#37629a"),
    "orange": ("#fff0e5", "#bd5e30"),
    "ink": ("#eaf0f2", "#1d4258"),
}


def card(x, y, w, title, lines=(), tone="teal", h=92):
    bg, stroke = COLORS[tone]
    s = f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="{bg}" stroke="{stroke}" stroke-width="1.3"/><rect x="{x}" y="{y + 12}" width="3" height="{h - 24}" fill="{stroke}"/><text x="{x + 16}" y="{y + 26}" fill="{stroke}" font-size="13" font-weight="700">{escape(title)}</text>'
    for i, line in enumerate(lines):
        s += f'<text x="{x + 16}" y="{y + 47 + i * 17}" fill="#4e6473" font-size="10">{escape(line)}</text>'
    return s


def arrow(x1, y1, x2, y2, dash=False):
    return (
        f'<path d="M{x1},{y1} L{x2},{y2}" stroke="#6a939d" stroke-width="1.5" fill="none" marker-end="url(#arr)"'
        + (' stroke-dasharray="4 4"' if dash else "")
        + "/>"
    )


def svg(body, h=330, label="Architecture diagram"):
    marker = "arr-" + hashlib.sha256(label.encode()).hexdigest()[:10]
    body = body.replace("url(#arr)", f"url(#{marker})")
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 900 {h}" role="img" aria-label="{escape(label)}"><defs><marker id="{marker}" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8" fill="#6a939d"/></marker></defs>{body}</svg>'


def figure(title, body, caption, h=330):
    return f'<figure><p class="diagram-title">{title}</p>{svg(body, h, title)}<figcaption>{caption}</figcaption></figure>'


def diagram(key):
    if key.startswith("dlc-"):
        return lifecycle_diagram(key)
    if key == "strategy":
        b = '<text x="24" y="25" font-size="11" fill="#68808a" letter-spacing="2">BUSINESS OUTCOME → BOUNDED AUTHORITY → MEASURABLE EVIDENCE</text>'
        for x, t, ls, c in [
            (
                24,
                "Business mandate",
                ["Outcome • owner • baseline", "Risk appetite • success measures"],
                "orange",
            ),
            (
                326,
                "Platform paved road",
                ["Typed package • CLI • adapters", "Reusable controls and templates"],
                "teal",
            ),
            (
                628,
                "Accountable operation",
                ["Catalog • telemetry • revocation", "Business and central oversight"],
                "blue",
            ),
        ]:
            b += card(x, 50, 248, t, ls, c, h=102)
        b += arrow(274, 101, 319, 101) + arrow(576, 101, 621, 101)
        b += card(
            24,
            205,
            852,
            "Feedback loop",
            [
                "Incidents, evaluation failures, cost and user outcomes update standards and golden templates."
            ],
            "ink",
            h=78,
        )
        b += arrow(752, 153, 752, 200) + arrow(151, 204, 151, 159)
        return figure(
            "01 / Strategy is an operating system",
            b,
            "Success is a governed business outcome. Standards and platform tools reduce the cost of producing and maintaining the evidence.",
            305,
        )
    if key == "lifecycle":
        b = ""
        titles = [
            ("Frame", "Owner, risks, acceptance"),
            ("Construct", "Factory + capability contract"),
            ("Evaluate", "Quality + deterministic gates"),
            ("Qualify", "Policy + runtime negatives"),
            ("Release", "Bind and review evidence"),
            ("Operate", "Monitor, revoke, improve"),
        ]
        for i, (t, description) in enumerate(titles):
            row = 0 if i < 3 else 1
            col = i if i < 3 else 5 - i
            x = 24 + col * 298
            y = 25 + row * 145
            b += card(
                x,
                y,
                255,
                f"0{i + 1}  {t}",
                [description, "Version every material change"],
                ["orange", "teal", "teal", "blue", "blue", "ink"][i],
            )
        b += (
            arrow(282, 70, 315, 70)
            + arrow(580, 70, 613, 70)
            + arrow(747, 119, 747, 162)
            + arrow(618, 215, 585, 215)
            + arrow(320, 215, 287, 215)
        )
        return figure(
            "02 / Development lifecycle",
            b,
            "Every transition has an owner, a bound evidence artifact and a failure path. Evaluation or policy changes invalidate the relevant evidence.",
            285,
        )
    if key in ("deployment", "compiler"):
        b = '<text x="24" y="21" font-size="10" fill="#567586" letter-spacing="1">DEVELOPER INTENT</text><text x="326" y="21" font-size="10" fill="#567586" letter-spacing="1">PLATFORM COMPILATION</text><text x="628" y="21" font-size="10" fill="#567586" letter-spacing="1">RELEASE + RUNTIME</text>'
        b += card(
            24,
            40,
            245,
            "Agent package",
            ["Code • manifest • eval cases", "Declared capabilities • owner"],
            "orange",
        )
        b += card(
            326,
            40,
            245,
            "Evidence + policy compiler",
            ["Static hints + observed deltas", "Org boundary ∩ approved request"],
            "teal",
        )
        b += card(
            628,
            40,
            248,
            "Immutable release bundle",
            ["Image + policy + evidence digests", "Signed provenance • catalog"],
            "blue",
        )
        b += arrow(273, 85, 320, 85) + arrow(575, 85, 622, 85)
        b += card(
            24,
            189,
            245,
            "Negative control tests",
            ["Bypass • revoke • expiry • replay", "Kernel / runtime prerequisites"],
            "ink",
        )
        b += card(
            326,
            189,
            245,
            "Independent qualification",
            ["Schema + enforcement + readiness", "Unsupported control = blocked"],
            "teal",
        )
        b += card(
            628,
            189,
            248,
            "Governed activation",
            ["Admission validates binding", "Monitor effective policy state"],
            "blue",
        )
        b += arrow(746, 133, 746, 160) + arrow(746, 160, 146, 160) + arrow(146, 160, 146, 182)
        b += arrow(273, 235, 320, 235) + arrow(575, 235, 622, 235)
        return figure(
            "03 / A compiler with evidence, not a YAML factory",
            b,
            "The target platform activates only a qualified bundle. The initial renderer deliberately starts at zero replicas; rendered resources do not prove enforcement.",
            315,
        )
    if key == "validation-lab":
        b = '<text x="24" y="22" font-size="11" fill="#567586" letter-spacing="1">HOST: ACTUAL READ-ONLY CHAIN</text>'
        b += card(
            24,
            42,
            250,
            "Agent + Temporal SDK",
            ["Real retry and history replay", "Known deterministic knowledge/MCP"],
            "orange",
        )
        b += card(
            326,
            42,
            250,
            "Native agentgateway",
            ["JWT + tool allow/deny tests", "Release / run / operation metadata"],
            "teal",
        )
        b += card(
            628,
            42,
            248,
            "Deterministic MCP receiver",
            ["Allowed read returns real result", "Forbidden write is not forwarded"],
            "blue",
        )
        b += arrow(278, 88, 320, 88) + arrow(580, 88, 622, 88)
        b += '<rect x="18" y="180" width="864" height="158" rx="12" fill="#edf2f6" stroke="#55778e"/><text x="35" y="207" font-size="11" fill="#375f78" letter-spacing="1">DISPOSABLE KIND: SEPARATE BOUNDARY FIXTURES</text>'
        b += card(
            35,
            224,
            242,
            "Tetragon + Cilium",
            ["Scoped file kill + network deny", "Unselected and allowed peers pass"],
            "ink",
        )
        b += card(
            331,
            224,
            242,
            "OpenShell supervisor",
            ["Strict filesystem/network probe", "Actual offline reference factory"],
            "teal",
        )
        b += card(
            628,
            224,
            242,
            "OpenTelemetry collector",
            ["Actual gateway spans received", "Correlation and denial assertions"],
            "blue",
        )
        b += arrow(451, 138, 451, 157) + arrow(451, 157, 749, 157) + arrow(749, 157, 749, 218)
        return figure(
            "08 / What the local evidence actually connects",
            b,
            "The gateway chain is live; kernel/CNI and OpenShell have separate fixtures. These observations do not qualify their full production composition. Diagnostic correlation is not workload authorization.",
            355,
        )
    if key == "risk":
        b = (
            card(
                25,
                50,
                225,
                "Declared + inferred facts",
                ["Data • action • autonomy", "Exposure • blast radius"],
                "orange",
            )
            + card(
                332,
                50,
                235,
                "Deterministic tier floor",
                ["Maximum applicable trigger", "Unknowns raise review floor"],
                "teal",
            )
            + card(
                651,
                50,
                222,
                "Human risk decision",
                ["Residual risk • exceptions", "Independent review when required"],
                "blue",
            )
        )
        b += arrow(254, 97, 326, 97) + arrow(571, 97, 645, 97)
        b += card(
            332,
            197,
            541,
            "Required controls + material scenarios",
            [
                "Control coverage is tested separately from inherent risk. Lower residual risk never erases the original tier."
            ],
            "ink",
            h=80,
        )
        b += arrow(762, 145, 762, 190)
        return figure(
            "04 / Risk classification produces obligations",
            b,
            "Tiering is conservative and explainable. An incomplete inventory, unsupported capability or uncertain data classification blocks automatic promotion.",
            300,
        )
    if key == "kill":
        b = card(
            28,
            126,
            232,
            "Authorized stop decision",
            ["Agent / release / tenant / run", "Reason • actor • monotonic epoch"],
            "orange",
            h=100,
        )
        b += (
            card(
                345,
                25,
                245,
                "Intake + workflow control",
                ["Reject new tasks; cancel work", "Bound cancellation timeout"],
                "teal",
            )
            + card(
                345,
                151,
                245,
                "Gateway + credentials",
                ["Revoke semantic permissions", "Expire capability / credential leases"],
                "teal",
            )
            + card(
                345,
                277,
                245,
                "Sandbox + workload",
                ["Contain process; stop pod", "Persist denied state across restart"],
                "teal",
            )
        )
        for y in [72, 199, 325]:
            b += f'<path d="M260,174 H302 V{y} H338" fill="none" stroke="#6a939d" stroke-width="1.5" marker-end="url(#arr)"/>'
        b += card(
            655,
            126,
            220,
            "Measured acknowledgement",
            ["Per layer effective-state epoch", "Missing ack → escalation"],
            "blue",
            h=100,
        )
        for y in [72, 199, 325]:
            b += f'<path d="M590,{y} H620 V174 H648" fill="none" stroke="#6a939d" stroke-width="1.5" marker-end="url(#arr)"/>'
        return figure(
            "05 / Kill switch means revoked authority",
            b,
            "Stopping a pod alone leaves queued workflows, cached permits and valid credentials. The control plane measures convergence and cannot undo already committed external actions.",
            393,
        )
    if key == "telemetry":
        b = ""
        for y, t, ls, c in [
            (
                20,
                "Agent & workflow",
                [
                    "run ID • operation ID • trace context",
                    "Quality, latency, retry and budget outcomes",
                ],
                "orange",
            ),
            (
                133,
                "Enforcement boundaries",
                [
                    "policy digest • identity • decision • rule ID",
                    "Gateway, sandbox and kernel event correlation",
                ],
                "teal",
            ),
            (
                246,
                "Release & catalog",
                [
                    "image digest • evidence digest • owner • tier",
                    "Approved state versus effective state",
                ],
                "blue",
            ),
        ]:
            b += card(24, y, 372, t, ls, c) + arrow(402, y + 46, 500, y + 46)
        b += card(
            509,
            70,
            360,
            "Redacted collection + access controls",
            [
                "No raw prompt content by default",
                "Tenant / business-function isolation",
                "Central fleet aggregate + authorized drill-down",
            ],
            "ink",
            h=190,
        )
        return figure(
            "06 / Three planes, one release identity",
            b,
            "Semantic traces, durable histories and kernel events are related records, not automatically one perfect DAG. Explicit context propagation and release identifiers make them joinable.",
            360,
        )
    if key == "boundary":
        b = '<rect x="20" y="15" width="578" height="382" rx="15" fill="#edf2f6" stroke="#55778e" stroke-width="1.3"/><text x="40" y="41" fill="#375f78" font-size="12" font-weight="700">CLUSTER / ADMISSION / EVIDENCE BINDING</text>'
        b += '<rect x="44" y="62" width="266" height="269" rx="12" fill="#e8f5f0" stroke="#278b80"/><text x="65" y="83" fill="#17786e" font-size="11" font-weight="700">SANDBOX / CONFINED AGENT</text><text x="65" y="100" fill="#17786e" font-size="10">eBPF evidence at node boundary</text><text x="635" y="87" fill="#37629a" font-size="11" font-weight="700">EXTERNAL DESTINATIONS</text>'
        b += (
            card(
                76,
                112,
                215,
                "Agent logic",
                [
                    "Typed factory + dependencies",
                    "No upstream credentials",
                    "Untrusted inputs remain data",
                ],
                "orange",
                h=131,
            )
            + card(
                346,
                112,
                218,
                "Governed gateway",
                [
                    "Authenticated model / MCP route",
                    "Tool and route authorization",
                    "Backend credential binding",
                ],
                "teal",
                h=131,
            )
            + card(
                619,
                112,
                203,
                "Tools + providers",
                [
                    "Resource-level authorization",
                    "Idempotent effects + approval",
                    "Tenant-bound credentials",
                ],
                "blue",
                h=131,
            )
        )
        b += arrow(296, 177, 340, 177) + arrow(568, 177, 613, 177)
        b += '<path d="M180,258 L735,258" stroke="#b55f3a" stroke-width="1.5" stroke-dasharray="5 5"/><text x="242" y="284" font-size="11" fill="#a45534">Direct egress / credential bypass must fail in negative tests</text>'
        b += card(
            65,
            344,
            770,
            "Outside the prompt: durable control + independent oversight",
            [
                "Temporal coordination • redacted telemetry • catalog evidence • revocation and effective-state acknowledgement"
            ],
            "ink",
            h=72,
        )
        out = figure(
            "07 / Trust is established at each boundary",
            b,
            "The agent process is not a trust root. Prompt instructions support behavior; identities, transport controls and backend authorization enforce authority.",
            434,
        )
        details = [
            (
                "agent",
                "Agent",
                "The portable factory gets narrow typed capabilities. In-process checks help developers but do not withstand arbitrary code execution; the sandbox and network boundary must enforce the same limits.",
            ),
            (
                "sandbox",
                "Sandbox + node",
                "OpenShell constrains the process/filesystem/network; Tetragon provides selected kernel telemetry and tested actions. Neither proves that an allowed business action is appropriate.",
            ),
            (
                "gateway",
                "Gateway",
                "The gateway authenticates workload identity and restricts model and MCP routes. Direct egress must be blocked. Protocol visibility, TLS trust and effective identity need explicit tests.",
            ),
            (
                "control",
                "Control plane",
                "Signed release binding, durable workflow control and catalog oversight govern deployment. Revocation must reach every boundary and report the effective policy epoch.",
            ),
        ]
        btns = "".join(
            f'<button data-layer="{k}" data-detail="{escape(d, quote=True)}" aria-pressed="{str(i == 0).lower()}">{t}</button>'
            for i, (k, t, d) in enumerate(details)
        )
        return f'<div class="boundary-widget">{out}<div class="diagram-controls">{btns}</div><div class="boundary-detail" aria-live="polite">{details[0][2]}</div></div>'
    return technical_diagram(key)


def hero_svg():
    return """<svg class="hero-svg" viewBox="0 0 430 365" role="img" aria-label="Zetra establishes bounded authority through concentric controls"><g fill="none" stroke-width="1"><circle cx="215" cy="170" r="153" stroke="#365563"/><circle cx="215" cy="170" r="121" stroke="#437a83"/><circle cx="215" cy="170" r="89" stroke="#469d98"/><circle cx="215" cy="170" r="56" stroke="#64e4cf" stroke-width="1.5"/><path d="M215 17V323 M62 170H368" stroke="#294958" stroke-dasharray="3 6"/></g><rect x="159" y="143" width="112" height="55" rx="6" fill="#1c423f" stroke="#64e4cf"/><text x="215" y="166" text-anchor="middle" fill="#e9fcf6" font-size="14" font-weight="700" letter-spacing="2">AGENT</text><text x="215" y="182" text-anchor="middle" fill="#9dd5c6" font-size="9">BOUNDED AUTHORITY</text><g font-size="10" fill="#b6d2d8"><rect x="148" y="8" width="136" height="21" fill="#16313f"/><text x="215" y="23" text-anchor="middle">GOVERNANCE + EVIDENCE</text><rect x="142" y="44" width="146" height="19" fill="#16313f"/><text x="215" y="58" text-anchor="middle">CLUSTER + IDENTITY</text><rect x="149" y="78" width="132" height="18" fill="#17363e"/><text x="215" y="91" text-anchor="middle">SANDBOX + GATEWAY</text></g><g fill="#64e4cf"><circle cx="88" cy="255" r="3"/><circle cx="322" cy="113" r="3"/><circle cx="178" cy="89" r="3"/></g><text x="215" y="352" text-anchor="middle" fill="#769fae" font-size="9" letter-spacing="2">EXPLICIT CONTRACTS · INDEPENDENT CONTROLS</text></svg>"""


def augment(fragment):
    fragment = re.sub(
        r'<div data-diagram="([^"]+)"></div>', lambda m: diagram(m.group(1)), fragment
    )
    fragment = re.sub(
        r"<table>(.*?)</table>",
        r'<div class="table-wrap"><table>\1</table></div>',
        fragment,
        flags=re.S,
    )

    def heading(m):
        if "id=" in m.group(1):
            return m.group(0)
        label = re.sub("<[^>]+>", "", m.group(2))
        ident = re.sub("[^a-z0-9]+", "-", label.lower()).strip("-")
        return f'<h3{m.group(1)} id="{ident}">{m.group(2)}</h3>'

    return re.sub(r"<h3([^>]*)>(.*?)</h3>", heading, fragment, flags=re.S)


def developer_guide_shell(title, lead, fragment):
    body = augment(fragment)
    steps = re.findall(r'<h3[^>]*id="(dlc-step-\d+)"[^>]*>(.*?)</h3>', body, re.S)
    links = []
    for index, (ident, heading) in enumerate(steps, 1):
        text = re.sub(r"^\d+\.\s*", "", re.sub("<[^>]+>", "", heading))
        links.append(
            f'<a href="#{ident}"><span class="nav-number">{index:02}</span>'
            f"<span>{escape(text)}</span></a>"
        )
    nav = "".join(
        f'<div class="nav-section"><p class="nav-group">{label}</p>'
        + "".join(links[start:end])
        + "</div>"
        for label, start, end in (("Build", 0, 4), ("Validate", 4, 7), ("Operate", 7, 9))
    )
    toc = "".join(
        f'<a href="#{ident}">{escape(re.sub("<[^>]+>", "", heading))}</a>'
        for ident, heading in steps
    )
    # Give each reading step a semantic section without changing source prose.
    matches = list(re.finditer(r'<h3[^>]*id="(dlc-step-\d+)"[^>]*>', body))
    if matches:
        pieces = [body[: matches[0].start()]]
        for index, match in enumerate(matches):
            end = (
                matches[index + 1].start() if index + 1 < len(matches) else body.rfind("</section>")
            )
            pieces.append(
                f'<section class="guide-step" aria-labelledby="{match.group(1)}">'
                + body[match.start() : end]
                + "</section>"
            )
        pieces.append(body[body.rfind("</section>") :])
        body = "".join(pieces)
    route = "".join(
        f'<a href="#dlc-step-{step}"><span class="route-number">{number}</span>'
        f'<span><span class="route-text">{label}</span><span class="route-range">{detail}</span></span></a>'
        for number, label, detail, step in (
            ("01", "Build", "Define & connect", 1),
            ("02", "Validate", "Test & improve", 5),
            ("03", "Operate", "Release & learn", 8),
        )
    )
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="{escape(lead, quote=True)}"><title>{escape(title)}</title>
<style>{CSS}\n{GUIDE_CSS}</style></head><body class="guide-theme">
<a href="#main" class="guide-skip">Skip to content</a>
<div class="reading-progress" aria-hidden="true"><span></span></div>
<button class="mobile-toggle" aria-label="Toggle navigation" aria-expanded="false">Contents</button>
<aside class="sidebar guide-nav" aria-label="Developer guide navigation">
<a class="brand" href="#lifecycle-guide">AGENT<br>DEVELOPER GUIDE</a>
<p class="brand-sub">From idea to operation</p>
<div class="edition">Developer playbook<br><strong>Practical steps · framework neutral</strong><br>09 October 2026</div>
<label class="nav-label" for="nav-search">Find a step</label>
<input id="nav-search" type="search" placeholder="Filter steps"><nav>{nav}</nav>
<p id="reading-position" class="reading-position">STEP 01 / 09</p>
<button class="detail-toggle" aria-expanded="false">Expand all details</button>
<button class="print">Print / Save as PDF</button>
<p class="note">Select a step to navigate. Expand examples and figure notes for more detail. Works offline.</p></aside>
<main id="main"><header class="hero guide-hero"><p class="eyebrow">A practical playbook</p>
<h1>{escape(title)}</h1><p class="lead">{escape(lead)}</p>
<p class="guide-scope">Nine steps · Checklists · Examples</p>
<nav class="hero-route" aria-label="Development phases">{route}</nav></header>
<article class="content guide-content"><details class="toc"><summary>Jump to a step</summary>{toc}</details>{body}</article>
<footer class="foot"><strong>Agent Developer Guide</strong> · 09 October 2026<br>
Use the examples as a starting point. Choose checks and limits that match your task, data and connected services.</footer></main>
<script>{JS}\n{GUIDE_JS}</script></body></html>'''


def shell(title, lead, fragments, combined=False):
    current = set()
    for fragment in fragments:
        match = re.search(r'<section id="([^"]+)"', fragment)
        if match is None:
            raise ValueError("Chapter fragment lacks a root section ID")
        current.add(match.group(1))
    if current == {"lifecycle-guide"}:
        return developer_guide_shell(title, lead, fragments[0])
    start_href = "#lifecycle-guide" if "lifecycle-guide" in current else "lifecycle-guide.html"
    nav = (
        f'<a class="start-here" href="{start_href}">Start here: Agent developer guide</a>'
        + "".join(
            f'<a href="{"#" + key if key in current else key + ".html"}"><small>{i + 1:02}</small>{escape(t)}</a>'
            for i, (key, t, _description) in enumerate(CHAPTERS)
            if key != "lifecycle-guide"
        )
    )
    guide = "".join(
        f'<a href="{"#" + k if k in current else k + ".html"}"><strong>{i + 1:02} / {escape(t.replace("Agent ", ""))}</strong><span>{escape(description)}</span></a>'
        for i, (k, t, description) in enumerate(CHAPTERS[:4])
    )
    technical_routes = "".join(
        f'<a href="{"#" + key if key in current else key + ".html"}">{escape(title)}</a>'
        for key, title, _ in CHAPTERS[6:8]
    )
    body = "\n".join(augment(f) for f in fragments)
    toc = "".join(
        f'<a href="#{id}">{escape(re.sub("<[^>]+>", "", label))}</a>'
        for id, label in re.findall(r'<h3[^>]*id="([^"]+)"[^>]*>(.*?)</h3>', body, re.S)
    )
    header = f"""<header class="hero"><p class="eyebrow">Bounded authority. Verifiable evidence.</p><div class="hero-grid"><div><h1>{escape(title)}</h1><p class="lead">{escape(lead)}</p><span class="badge">STRATEGY → DEVELOPMENT → DEPLOYMENT → OVERSIGHT</span><span class="badge">ZERO TRUST BY CONSTRUCTION</span></div>{hero_svg()}</div><p class="strap">A governed software package, a consistent lifecycle, and independent enforcement at every consequential boundary.</p></header>"""
    reading = f'''<div class="reading-guide"><p class="guide-entry">New to agent development? <a href="{start_href}">Follow the agent developer guide</a>.</p><p class="eyebrow">Choose your lens</p><div class="guide-grid">{guide}</div><p class="technical-route"><strong>Technical leader review:</strong> {technical_routes}</p></div>'''
    status_note = '<div class="callout"><strong>Status and reading convention.</strong> This is a proposed enterprise standard with a working initial toolkit. Upstream documentation checks, local tests and live enforcement evidence are different validation levels. See the toolkit chapter and integration report before treating a control as qualified.</div>'
    footer = '<footer class="foot"><strong>ZETRA / ZEro-TRust Agents</strong> · Design draft · 08 October 2026<br>Source chapters and build script live in the repository. Vendor integrations require a version-pinned qualification report; policy intent is never proof of runtime enforcement.</footer>'
    publication_date = "08 October 2026"
    toc_label = "In this document · detailed contents"
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="description" content="{escape(lead, quote=True)}"><title>{escape(title)} | Zetra</title><style>{CSS}</style></head><body><a href="#main" style="position:absolute;left:-9999px" onfocus="this.style.left='10px'" onblur="this.style.left='-9999px'">Skip to content</a><button class="mobile-toggle" aria-label="Toggle navigation" aria-expanded="false">Contents</button><aside class="sidebar" aria-label="Playbook navigation"><a class="brand" href="index.html">ZETRA</a><p class="brand-sub">ZEro-TRust Agents</p><div class="edition">Agent Playbook<br><strong>Design draft / toolkit v0.1</strong><br>{publication_date}</div><label class="nav-label" for="nav-search">Find a playbook</label><input id="nav-search" type="search" placeholder="Filter navigation"><p class="nav-label">Audience tracks</p><nav>{nav}<a href="index.html">Complete playbook</a></nav><p class="note">Framework-neutral Python<br>Temporal · Kubernetes<br>General enterprise baseline</p><button class="print">Print / Save as PDF</button><p class="note">Standalone HTML. Diagrams, code and styling work offline.</p></aside><main id="main">{header}{reading}<article class="content">{status_note}<details class="toc"><summary>{toc_label}</summary>{toc}</details>{body}</article>{footer}</main><script>{JS}</script></body></html>'''


def main():
    fragments = []
    for k, t, description in CHAPTERS:
        p = DOCS / "chapters" / f"{k}.html"
        if k == "source-review":
            p = DOCS / "research/source-review.html"
        if not p.exists():
            raise SystemExit(f"Missing chapter: {p}")
        f = p.read_text()
        fragments.append(f)
        (DOCS / f"{k}.html").write_text(shell(t, description, [f]))
    (DOCS / "index.html").write_text(
        shell(
            "The Agent Playbook",
            "Build agents that deliver useful outcomes, operate within explicit authority, and carry the evidence needed to deploy, manage and govern them.",
            fragments,
            True,
        )
    )
    print(f"Built {len(fragments) + 1} self-contained HTML documents.")


if __name__ == "__main__":
    main()
