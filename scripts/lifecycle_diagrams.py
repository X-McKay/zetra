"""Original, accessible vector explanations for the short developer guide."""

from html import escape
from math import cos, radians, sin

INK = "#111111"
WHITE = "#ffffff"
PAPER = "#f4f3f0"
ACCENT = "#e34420"
PURPLE = "#7030ba"
ORANGE_TEXT = "#b63818"
GRAY = "#6b6b68"
RULE = "#d8d7d2"


def text(x, y, value, size=16, color=INK, weight=400, anchor="start"):
    return (
        f'<text x="{x}" y="{y}" font-size="{max(14, size)}" fill="{color}" '
        f'font-weight="{weight}" text-anchor="{anchor}">{escape(value)}</text>'
    )


def rect(x, y, width, height, fill=PAPER, stroke="none", radius=0):
    return (
        f'<rect x="{x}" y="{y}" width="{width}" height="{height}" '
        f'rx="{radius}" fill="{fill}" stroke="{stroke}"/>'
    )


def path(points, color=INK, arrow=True, dashed=False):
    route = "M" + " L".join(f"{x},{y}" for x, y in points)
    marker = f' marker-end="url(#ARROW-{color[1:]})"' if arrow else ""
    dash = ' stroke-dasharray="5 5"' if dashed else ""
    return (
        f'<path d="{route}" fill="none" stroke="{color}" stroke-width="2" '
        f'stroke-linejoin="round"{marker}{dash}/>'
    )


def circle(x, y, radius, fill=PAPER, stroke="none"):
    return f'<circle cx="{x}" cy="{y}" r="{radius}" fill="{fill}" stroke="{stroke}"/>'


def icon(name, x, y, color=INK, scale=1):
    drawings = {
        "document": '<path d="M6 3h13l7 7v21H6z M19 3v8h7 M11 17h10 M11 22h10 M11 27h7"/>',
        "tool": '<path d="M11 8L3 17l8 9 M23 8l8 9-8 9 M20 5l-6 25"/>',
        "shield": '<path d="M17 3L4 8v9c0 8 13 15 13 15s13-7 13-15V8z M10 17l5 5 9-10"/>',
        "application": '<rect x="3" y="5" width="28" height="23" rx="2"/><path d="M3 11h28 M8 8h1 M12 8h1 M16 16l-5 4 5 4 M21 16l5 4-5 4"/>',
        "server": '<rect x="4" y="4" width="26" height="10" rx="2"/><rect x="4" y="20" width="26" height="10" rx="2"/><path d="M9 9h2 M17 9h8 M9 25h2 M17 25h8 M17 14v6"/>',
        "play": '<path d="M10 5l20 12-20 12z"/>',
        "score": '<path d="M5 7l3 3 5-6 M17 7h12 M5 19l3 3 5-6 M17 19h12 M5 30h8 M17 30h12"/>',
        "eye": '<path d="M2 17s6-10 15-10 15 10 15 10-6 10-15 10S2 17 2 17z"/><circle cx="17" cy="17" r="4"/>',
        "search": '<circle cx="14" cy="14" r="9"/><path d="M21 21l10 10"/>',
        "stop": '<rect x="6" y="6" width="22" height="22"/>',
        "check": '<path d="M5 17l8 8L29 8"/>',
        "release": '<path d="M4 11l13-7 13 7v15l-13 7-13-7z M4 11l13 7 13-7 M17 18v15"/>',
        "compare": '<path d="M5 5v24h25 M10 23h5v-9h-5z M21 23h5V8h-5z"/>',
    }
    return (
        f'<g transform="translate({x} {y}) scale({scale})" fill="none" '
        f'stroke="{color}" stroke-width="1.8" stroke-linecap="round" '
        f'stroke-linejoin="round">{drawings[name]}</g>'
    )


def figure(key, title, body, height, caption, description, companion=""):
    prefix = "lifecycle-" + key
    markers = ""
    for color in (INK, ACCENT, GRAY, PURPLE, ORANGE_TEXT):
        identifier = f"{prefix}-arrow-{color[1:]}"
        body = body.replace(f"ARROW-{color[1:]}", identifier)
        markers += (
            f'<marker id="{identifier}" markerWidth="9" markerHeight="9" '
            'refX="8" refY="4.5" orient="auto" markerUnits="userSpaceOnUse">'
            f'<path d="M0 0L9 4.5L0 9" fill="{color}"/></marker>'
        )
    return (
        f'<figure class="technical-diagram lifecycle-diagram" data-diagram-key="{key}">'
        f'<p class="diagram-title">{escape(title)}</p>'
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1080 {height}" '
        f'role="img" aria-label="{escape(title)}" aria-describedby="{prefix}-description" '
        'style="display:block;width:100%;height:auto;font-family:Arial,Helvetica,sans-serif">'
        f'<title>{escape(title)}</title><desc id="{prefix}-description">{escape(description)}</desc>'
        f"<defs>{markers}</defs><style>a:hover .step-surface,a:focus .step-surface{{stroke:{ACCENT};stroke-width:2}}"
        f"a:focus{{outline:none}}</style>{rect(0, 0, 1080, height, WHITE)}{body}</svg>"
        f"<figcaption>{escape(caption)}</figcaption>{companion}</figure>"
    )


def overview():
    key = "dlc-overview"
    stages = [
        (132, 91, "Define the problem", "A useful outcome + limits"),
        (404, 91, "Organize the repository", "A clear home for the work"),
        (676, 91, "Add skills & tools", "Approved operations"),
        (948, 91, "Connect MCP", "Only where it helps"),
        (948, 262, "Test the software", "Allowed + forbidden paths"),
        (540, 262, "Evaluate results", "Evidence of useful work"),
        (132, 262, "Optimize", "Quality, speed, cost"),
        (132, 433, "Version & release", "Both gates; start small"),
        (948, 433, "Monitor & improve", "Failures become checks"),
    ]
    body = text(36, 34, "BUILD", 15, PURPLE, 700)
    body += text(1044, 205, "VALIDATE", 15, PURPLE, 700, "end")
    body += text(36, 376, "OPERATE", 15, PURPLE, 700)
    body += path([(157, 91), (379, 91)], PURPLE)
    body += path([(429, 91), (651, 91)], PURPLE)
    body += path([(701, 91), (923, 91)], PURPLE)
    body += path([(973, 91), (1044, 91), (1044, 262), (973, 262)], PURPLE)
    body += path([(923, 262), (565, 262)], PURPLE)
    body += path([(515, 262), (157, 262)], PURPLE)
    body += path([(107, 262), (26, 262), (26, 433), (107, 433)], PURPLE)
    body += path([(157, 433), (923, 433)], PURPLE)
    links = []
    for step, (x, y, title, detail) in enumerate(stages, 1):
        body += f'<a href="#dlc-step-{step}" aria-label="Go to step {step}: {escape(title)}" tabindex="0">'
        body += (
            f'<circle class="step-surface" cx="{x}" cy="{y}" r="23" fill="{INK}" stroke="{INK}"/>'
        )
        body += text(x, y + 6, str(step), 18, WHITE, 700, "middle")
        body += text(x, y + 59, title, 18, weight=700, anchor="middle")
        body += text(x, y + 86, detail, 14, GRAY, anchor="middle")
        body += "</a>"
        links.append(f'<li><a href="#dlc-step-{step}">{step}. {escape(title)}</a></li>')
    body += path([(973, 433), (1064, 433), (1064, 298), (948, 298), (948, 290)], GRAY, dashed=True)
    body += text(540, 462, "Every change returns to the checks", 16, GRAY, anchor="middle")
    companion = (
        '<nav class="diagram-step-links" aria-label="Choose a lifecycle step"><ol>'
        + "".join(links)
        + "</ol></nav>"
    )
    return figure(
        key,
        "Build → validate → operate",
        body,
        548,
        "Follow the task through nine steps. Select a step to jump to its guidance.",
        "Build steps one to four define the problem, repository, tools and connections. Validate steps five to seven test, evaluate and optimize. Operate steps eight and nine release and monitor. Changes return to the checks.",
        companion,
    )


def skills_tools():
    key = "dlc-skills-tools"
    body = text(36, 42, "QUESTION", 14, GRAY, 700)
    body += text(36, 76, "What receipt is needed?", 28, weight=700)
    body += rect(36, 130, 295, 241, PAPER)
    body += text(56, 167, "Skill / answer from documents", 18, weight=700)
    for i, line in enumerate(
        ("Read approved sources", "Compare the evidence", "Cite or ask for help")
    ):
        body += text(56, 214 + 42 * i, f"0{i + 1}", 14, PURPLE, 700)
        body += text(87, 214 + 42 * i, line, 17)
    body += text(56, 348, "A procedure, not access rights", 14, GRAY)
    body += path([(341, 229), (391, 229)], GRAY, dashed=True)
    body += text(344, 208, "guides", 14, GRAY)
    body += icon("tool", 419, 149, PURPLE)
    body += text(419, 213, "Tool call", 23, weight=700)
    body += text(419, 246, "read_document(id)", 20, PURPLE, 700)
    body += text(419, 278, "Validate arguments + limits", 14, GRAY)
    body += path([(640, 229), (714, 229)])
    body += path([(731, 122), (731, 381)], ACCENT, arrow=False)
    body += icon("shield", 714, 79, ACCENT)
    body += text(755, 106, "Permission boundary", 19, weight=700)
    body += text(755, 138, "Actual caller + resource", 14, GRAY)
    body += path([(738, 229), (775, 229), (775, 190), (810, 190)], PURPLE)
    body += icon("check", 822, 166, PURPLE, 0.75)
    body += text(857, 191, "expense-policy", 18, weight=700)
    body += text(857, 218, "Allowed → read", 15, PURPLE)
    body += path([(775, 229), (775, 312), (810, 312)], ACCENT)
    body += icon("stop", 822, 289, ACCENT, 0.75)
    body += text(857, 313, "private-budget", 18, weight=700)
    body += text(857, 340, "Denied → no contents", 15, ORANGE_TEXT)
    body += text(
        540,
        427,
        "Instructions can guide a call. They cannot authorize it.",
        20,
        weight=700,
        anchor="middle",
    )
    return figure(
        key,
        "The same tool. Different permissions. Different outcomes.",
        body,
        459,
        "Illustrative document request: enforced permission checks decide which information is returned.",
        "A policy question uses a skill to guide a read-document tool. Validated arguments reach a permission boundary checking the actual caller and resource. An approved expense policy may be read; a denied private budget returns no contents. Instructions cannot grant access.",
    )


def mcp():
    key = "dlc-mcp"
    body = ""
    for x, title, subtitle, shape in [
        (36, "Application + client", "Chooses needed capabilities", "application"),
        (390, "MCP server", "Exposes reviewed operations", "server"),
        (744, "Source service", "Owns data and access checks", "shield"),
    ]:
        body += path([(x, 108), (x + 300, 108)], PURPLE, arrow=False)
        body += icon(shape, x + 15, 51, ACCENT if shape == "shield" else INK, 0.9)
        body += text(x + 56, 65, title, 18, weight=700)
        body += text(x + 56, 89, subtitle, 12, GRAY)
        body += path([(x + 150, 120), (x + 150, 393)], RULE, arrow=False, dashed=True)
    routes = [
        (166, 186, 540, "01", 'search_documents("receipt")'),
        (236, 540, 894, "02", "Search only approved documents"),
        (306, 894, 540, "03", "expense-policy + source reference"),
        (376, 540, 186, "04", "Evidence for a sourced answer"),
    ]
    for y, start, end, number, label in routes:
        color = ORANGE_TEXT if number == "02" else INK
        body += text(min(start, end) + 14, y - 15, number, 14, color, 700)
        body += text(min(start, end) + 45, y - 15, label, 15, color)
        body += path([(start, y), (end, y)], color)
    body += rect(36, 428, 1008, 51, INK)
    body += text(
        540,
        460,
        "Answer: an itemized receipt is required. Source: expense-policy.",
        18,
        WHITE,
        700,
        "middle",
    )
    return figure(
        key,
        "One connection. Two directions. Access still checked.",
        body,
        499,
        "Example connection: an approved MCP document server and its source service. Unrelated tools stay disabled.",
        "The client requests an operation through an MCP server. The source service checks the actual caller and resource, then returns permitted data or a denial. A result or error returns to the client. Protocol connectivity does not itself authorize access.",
    )


def evaluations():
    key = "dlc-evaluations"
    body = text(36, 40, "EXAMPLE COVERAGE PLAN", 14, PURPLE, 700)
    headings = [
        (36, "Case"),
        (361, "Evidence"),
        (505, "Access"),
        (649, "Uncertainty"),
        (793, "Effects"),
        (930, "Expected outcome"),
    ]
    for x, label in headings:
        body += text(x, 89, label, 16, GRAY, 700, "start" if x == 36 else "middle")
    body += path([(36, 110), (1044, 110)], INK, arrow=False)
    rows = [
        ("Receipt requirement", [True, True, False, True], "Answer + cite"),
        ("Missing policy", [True, True, True, True], "Ask for help"),
        ("Restricted document", [False, True, False, True], "No contents"),
        ("Instructions in a document", [True, True, False, True], "Stay within scope"),
    ]
    for i, (case, checks, outcome) in enumerate(rows):
        y = 153 + i * 65
        body += text(36, y, case, 17, weight=700 if i == 0 else 400)
        for x, required in zip((361, 505, 649, 793), checks, strict=True):
            body += (
                circle(x, y - 5, 6, PURPLE)
                if required
                else text(x, y, "—", 17, GRAY, anchor="middle")
            )
        body += text(930, y, outcome, 16, ORANGE_TEXT if i == 2 else INK, 700, "middle")
        body += path([(36, y + 23), (1044, y + 23)], RULE, arrow=False)
    body += circle(43, 407, 6, PURPLE)
    body += text(60, 413, "Required check", 14, GRAY)
    body += text(252, 413, "—", 17, GRAY)
    body += text(277, 413, "Not applicable", 14, GRAY)
    body += text(
        540,
        469,
        "Run → inspect answers and actual actions → score each case",
        20,
        weight=700,
        anchor="middle",
    )
    return figure(
        key,
        "Which cases exercise which boundaries?",
        body,
        498,
        "This is an expected-check plan, not measured results. Repeat model trials; count failures and timeouts in success rates.",
        "A coverage matrix links policy questions, missing evidence, restricted documents and misleading document instructions to evidence, access, uncertainty and effect checks. Dots mean required checks; dashes mean not applicable. Expected outcomes are a cited answer, review, denied contents or staying within scope. These are planned assertions, not observed benchmark results.",
    )


def release():
    key = "dlc-release"
    body = text(36, 39, "ILLUSTRATIVE QUALITY GATES", 14, PURPLE, 700)
    body += text(36, 72, "Baseline 92% → candidate 90%", 27, weight=700)

    # Percentage score uses a common horizontal scale: 88–94%.
    def score_x(score):
        return 72 + (score - 88) * 90

    absolute, relative, baseline = score_x(90), score_x(91), score_x(92)
    body += path([(72, 341), (612, 341)], RULE, arrow=False)
    for score in range(88, 95):
        x = score_x(score)
        body += path([(x, 337), (x, 347)], RULE, arrow=False)
        body += text(x, 374, f"{score}%", 14, GRAY, anchor="middle")
    body += path([(absolute, 100), (absolute, 331)], ACCENT, arrow=False)
    body += path([(relative, 100), (relative, 331)], PURPLE, arrow=False, dashed=True)
    body += circle(baseline, 135, 8, INK)
    body += text(baseline + 20, 141, "92% baseline", 18, weight=700)
    body += path([(baseline - 5, 144), (absolute + 5, 287)], PURPLE, arrow=False)
    body += circle(absolute, 294, 9, PURPLE)
    body += text(72, 300, "90% candidate", 18, PURPLE, 700)
    body += path([(212, 294), (237, 294)], GRAY, arrow=False)
    body += text(390, 229, "−2 percentage points", 18, GRAY)
    body += text(absolute, 408, "90% minimum", 15, ORANGE_TEXT, 700, "middle")
    body += text(relative + 20, 438, "91% relative floor = baseline − 1 point", 15, PURPLE, 700)
    body += path([(690, 104), (690, 438)], RULE, arrow=False)
    body += text(730, 132, "DECISION", 14, GRAY, 700)
    for y, label, outcome, color in [
        (185, "Absolute minimum", "Pass", PURPLE),
        (241, "Relative limit", "Fail", ORANGE_TEXT),
    ]:
        body += text(730, y, label, 18)
        body += text(1044, y, outcome, 19, color, 700, "end")
        body += path([(730, y + 17), (1044, y + 17)], RULE, arrow=False)
    body += text(730, 303, "Required safety checks", 18, weight=700)
    body += text(730, 331, "Must also pass independently", 14, GRAY)
    body += text(730, 397, "HOLD", 40, ACCENT, 700)
    body += text(730, 426, "The relative gate fails.", 18)
    return figure(
        key,
        "Meeting the minimum is not enough.",
        body,
        473,
        "Example thresholds from the guide, not measured model results. Both quality gates and required safety checks must pass.",
        "On a common percentage scale, the approved baseline is 92 percent and the candidate is 90 percent. The absolute minimum is 90 percent, which the candidate meets. A maximum one-percentage-point decline sets a 91-percent relative floor, which it misses. The release is held; required safety checks remain independent.",
    )


def operate():
    key = "dlc-operate"
    nodes = [
        ("Observe", "Notice the missing source"),
        ("Diagnose", "Trace run + release IDs"),
        ("Contain", "Pause when required"),
        ("Add a regression", "Uncited answers must fail"),
        ("Fix & evaluate", "Check answer + evidence"),
        ("Review & release", "Verify the new behavior"),
    ]
    positions = []
    body = ""
    for i in range(6):
        angle = -90 + i * 60
        positions.append((540 + 350 * cos(radians(angle)), 260 + 150 * sin(radians(angle))))
        start, end = radians(angle + 7), radians(angle + 53)
        x1, y1 = 540 + 350 * cos(start), 260 + 150 * sin(start)
        x2, y2 = 540 + 350 * cos(end), 260 + 150 * sin(end)
        color = ACCENT if i == 1 else PURPLE
        body += f'<path d="M{x1:.1f},{y1:.1f} A350,150 0 0 1 {x2:.1f},{y2:.1f}" fill="none" stroke="{color}" stroke-width="2" marker-end="url(#ARROW-{color[1:]})"/>'
    for i, ((title, detail), (x, y)) in enumerate(zip(nodes, positions, strict=True)):
        body += circle(round(x, 1), round(y, 1), 17, ORANGE_TEXT if i == 2 else PURPLE)
        body += text(round(x, 1), round(y + 5, 1), str(i + 1), 14, WHITE, 700, "middle")
        if i == 0:
            tx, ty, anchor = x, y - 45, "middle"
        elif i in (1, 2):
            tx, ty, anchor = x + 33, y - 7, "start"
        elif i == 3:
            tx, ty, anchor = x, y + 46, "middle"
        else:
            tx, ty, anchor = x - 33, y - 7, "end"
        body += text(round(tx, 1), round(ty, 1), title, 19, weight=700, anchor=anchor)
        body += text(round(tx, 1), round(ty + 27, 1), detail, 14, GRAY, anchor=anchor)
    body += text(540, 216, "EXAMPLE FAILURE", 14, GRAY, 700, "middle")
    body += text(540, 260, "A policy answer without a source", 27, weight=700, anchor="middle")
    body += text(540, 294, "One failure becomes a repeatable check.", 16, GRAY, anchor="middle")
    return figure(
        key,
        "Close the loop on one real problem.",
        body,
        513,
        "Illustrative response loop. Protect private content; reconcile uncertain effects before retrying. A stop is not an undo.",
        "An illustrative uncited policy answer is observed and traced to its run and release. Work is contained when necessary, the missing citation becomes a regression case, a fix is evaluated, and the reviewed change is released and observed again. This is an example response plan, not recorded telemetry.",
    )


def tests():
    key = "dlc-test-loop"
    body = text(36, 40, "ONE REQUEST, DIFFERENT TEST SCOPES", 14, PURPLE, 700)
    body += text(36, 78, "What receipt is needed?", 27, weight=700)
    body += path([(70, 144), (70, 121), (1010, 121), (1010, 144)], GRAY, arrow=False)
    body += rect(320, 103, 465, 32, WHITE)
    body += text(
        552, 125, "End-to-end / question → sourced answer", 18, weight=700, anchor="middle"
    )
    stages = [
        (112, "Request"),
        (330, "Validate"),
        (548, "Read tool"),
        (766, "Document service"),
        (984, "Answer"),
    ]
    for i, (x, label) in enumerate(stages):
        body += circle(x, 233, 8, INK)
        body += text(x, 268, label, 17, weight=700, anchor="middle")
        if i < 4:
            body += path([(x + 17, 233), (x + 200, 233)], PURPLE)
    body += path([(260, 205), (260, 174), (400, 174), (400, 205)], GRAY, arrow=False)
    body += text(330, 163, "Unit / valid inputs", 15, GRAY, anchor="middle")
    body += path([(500, 295), (500, 319), (820, 319), (820, 295)], GRAY, arrow=False)
    body += text(660, 346, "Integration / allowed + denied reads", 16, GRAY, anchor="middle")
    body += text(36, 404, "Smoke check", 18, PURPLE, 700)
    body += text(215, 404, "Starts and returns one basic valid response", 17)
    return figure(
        key,
        "Test the pieces and the complete result.",
        body,
        436,
        "An example test plan: verify the final result and effects, not just whether a call returned.",
        "A single policy request passes through validation, a read tool and document service to a sourced answer. A unit scope checks validation, an integration scope checks permitted and denied reads, an end-to-end scope checks the whole request, and a smoke check verifies basic startup and response. These are planned scopes, not test results.",
    )


def lifecycle_diagram(key):
    diagrams = {
        "dlc-overview": overview,
        "dlc-skills-tools": skills_tools,
        "dlc-mcp": mcp,
        "dlc-evaluations": evaluations,
        "dlc-release": release,
        "dlc-operate": operate,
        "dlc-test-loop": tests,
    }
    try:
        render = diagrams[key]
    except KeyError as exc:
        raise ValueError(f"Unknown lifecycle diagram: {key}") from exc
    return render()
