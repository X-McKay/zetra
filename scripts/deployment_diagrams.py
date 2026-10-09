"""Reading diagrams for the deployment guide; local SVG, no external assets."""

from html import escape


def text(x, y, value, size=16, color="#27252b", weight=400):
    return f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" font-weight="{weight}">{escape(value)}</text>'


def diagram(key):
    purple, orange, gray = "#71509c", "#ce692c", "#77747b"
    body = ""
    if key == "deploy-sequence":
        title, height = "The deployment sequence: artifacts first, authority last", 525
        for x, label, sub in [
            (65, "Azure Repos", "Source + GitOps"),
            (285, "Azure Pipelines", "Trusted CI"),
            (535, "ACR", "Images + bundles"),
            (790, "Argo CD", "Kubernetes reconciliation"),
        ]:
            body += text(x - 40, 30, label, 17, purple, 700) + text(x - 40, 54, sub, 14, gray)
            body += f'<path d="M{x} 75V451" stroke="#dedbd7" stroke-dasharray="3 5"/>'

        def transfer(x1, x2, y, label, color=purple):
            end = x2 - 8 if x2 > x1 else x2 + 8
            tip = (
                f"{end - 7},{y - 4} {end},{y} {end - 7},{y + 4}"
                if x2 > x1
                else f"{end + 7},{y - 4} {end},{y} {end + 7},{y + 4}"
            )
            return (
                f'<path d="M{x1} {y}H{end}" stroke="{color}" stroke-width="2"/><polyline points="{tip}" fill="none" stroke="{color}" stroke-width="2"/>'
                + text(min(x1, x2) + 12, y - 12, label, 14, color)
            )

        body += transfer(65, 285, 111, "1 Push → validate")
        body += transfer(285, 535, 176, "2 Publish candidate image")
        body += '<path d="M285 215C365 215 365 256 285 256" fill="none" stroke="#71509c" stroke-width="2"/>'
        body += text(382, 235, "3 Evaluate + profile", 17, purple, 700) + text(
            382, 258, "Compile + qualify policies", 14
        )
        body += transfer(285, 535, 310, "4 Seal + store bundle")
        body += transfer(285, 65, 380, "5 Approved GitOps merge", orange)
        body += transfer(
            65,
            790,
            426,
            "6 Argo detects desired state; installs bound policies and workload",
            orange,
        )
        body += text(
            370, 485, "7 Verify effective controls → canary → live authority", 17, orange, 700
        )
        caption = "ACR receives the candidate image before profiling. Only the approved GitOps merge triggers production reconciliation; runtime verification precedes activation."
    elif key == "deploy-artifacts":
        title, height = "One release identity binds the artifacts and their consumers", 420
        for x, y, label, sub in [
            (32, 62, "Image · ACR", "Immutable container digest"),
            (32, 285, "Evidence · object storage", "Versioned private records"),
            (655, 62, "Policies · ACR bundle", "Native configuration digests"),
            (655, 285, "Environment binding", "Identity + routes + runtime profile"),
        ]:
            body += text(x, y, label, 17, purple, 700) + text(x, y + 25, sub, 14)
        body += '<path d="M275 82L425 188 M275 290L425 222 M525 188L645 82 M525 222L645 290" fill="none" stroke="#b9a4d3" stroke-width="2"/>'
        body += (
            '<circle cx="475" cy="205" r="57" fill="#f0eaf6" stroke="#71509c" stroke-width="2"/>'
        )
        body += text(436, 200, "Release", 18, purple, 700) + text(
            436, 223, "digest", 18, purple, 700
        )
        body += text(40, 181, "Approval record", 17, orange, 700) + text(
            40, 204, "Bundle + environment scope", 14
        )
        body += '<path d="M285 199H408 M542 205H649" stroke="#ce692c" stroke-width="2"/>'
        body += text(660, 191, "GitOps projection", 17, orange, 700) + text(
            660, 216, "Exact files + digest checks", 14
        )
        body += '<path d="M475 262V328" stroke="#71509c"/>'
        body += text(344, 356, "Agent catalog indexes this release", 17, purple, 700)
        body += text(344, 382, "It does not replace the artifact or approval store.", 14)
        caption = "Store payloads once, address immutable versions, and carry the same release identity through approval, GitOps, policy loading, and runtime events."
    elif key == "deploy-states":
        title, height = "Release state advances only on recorded evidence", 215
        labels = [
            (40, "Candidate", "CI inputs valid"),
            (208, "Qualified", "Tests + controls pass"),
            (382, "Approved", "Scoped release approval"),
            (583, "Staged", "Installed; no live work"),
            (777, "Active", "Canary accepted"),
        ]
        body += '<path d="M40 81H822" stroke="#b9a4d3" stroke-width="2"/>'
        for x, label, sub in labels:
            body += f'<circle cx="{x}" cy="81" r="5" fill="{purple}"/>'
            body += text(x - 10, 54, label, 17, purple, 700) + text(x - 10, 116, sub, 14)
        body += '<path d="M40 145H822" stroke="#ce692c" stroke-dasharray="3 5"/>'
        body += text(
            135,
            181,
            "Failed or revoked: withhold authority, preserve evidence, reconcile effects.",
            16,
            orange,
            700,
        )
        caption = "A published or installed release is not yet active. Revocation is independent of GitOps reconciliation and prevents automatic reactivation."
    elif key == "deploy-lifecycle":
        title, height = "Two paths, one reviewed release", 360
        body += text(28, 34, "DEVELOPMENT", 14, purple, 700)
        body += text(28, 191, "DEPLOYMENT", 14, orange, 700)
        body += f'<path d="M40 91H480 Q535 91 535 150V180 Q535 248 420 248" fill="none" stroke="{purple}" stroke-width="3"/>'
        body += f'<path d="M40 248H852" fill="none" stroke="{orange}" stroke-width="3"/>'
        for x, label, sub in [
            (40, "Define", "Purpose + limits"),
            (224, "Build", "Package + tools"),
            (420, "Test + evaluate", "Quality + behavior"),
        ]:
            body += f'<circle cx="{x}" cy="91" r="6" fill="{purple}"/>'
            body += text(x - 12, 65, label, 17, purple, 700) + text(x - 12, 126, sub, 14)
        for x, label, sub in [
            (40, "Prepare", "Identity + services"),
            (224, "Profile", "Observed access"),
            (420, "Review", "Policies + evidence"),
            (675, "Release", "Verified controls"),
            (795, "Operate", "Monitor + stop"),
        ]:
            body += f'<circle cx="{x}" cy="248" r="6" fill="{orange}"/>'
            body += text(x - 12, 223, label, 17, orange, 700) + text(x - 12, 280, sub, 14)
        body += text(551, 161, "Image + evidence", 14, purple)
        body += (
            f'<path d="M811 300V332H224V140" fill="none" stroke="{gray}" stroke-dasharray="4 5"/>'
        )
        body += '<path d="M218 149L224 139L230 149" fill="none" stroke="#77747b"/>'
        body += text(340, 325, "Production findings become new test cases", 14, gray)
        caption = "Read across each path. Development evidence joins deployment review; operations feeds the next development cycle."
    elif key == "deploy-evidence":
        title, height = "Evidence narrows the proposal; review grants authority", 350
        rows = [
            (65, "Declared need", "Approved tools, data, scope", purple),
            (147, "Observed behavior", "Tests + evaluations + runtime events", purple),
            (229, "Organization limits", "Prohibited access + approval rules", orange),
        ]
        for y, label, sub, color in rows:
            body += text(28, y, label, 18, color, 700) + text(28, y + 25, sub, 15)
            body += f'<path d="M365 {y - 5}C450 {y - 5} 450 142 500 142" fill="none" stroke="{color}" stroke-width="2"/>'
        body += (
            f'<circle cx="538" cy="142" r="35" fill="#f3eef8" stroke="{purple}" stroke-width="2"/>'
        )
        body += text(514, 148, "Review", 14, purple, 700)
        for y, label in [
            (59, "Tetragon policy"),
            (126, "OpenShell policy"),
            (193, "Gateway authorization"),
            (260, "Network + admission"),
        ]:
            body += f'<path d="M573 142C620 142 620 {y} 650 {y}" fill="none" stroke="{purple}"/>'
            body += text(660, y + 5, label, 17, purple, 700)
        body += text(
            28,
            324,
            "Denied and unexplained events stay outside permission candidates.",
            16,
            orange,
            700,
        )
        caption = "The profile supports a permission review. Separate native artifacts enforce separate parts of the approved plan."
    elif key == "deploy-runtime":
        title, height = "Follow one request through the runtime boundaries", 440
        body += (
            '<rect x="28" y="38" width="470" height="310" rx="8" fill="#f7f6f3" stroke="#c8c5c1"/>'
        )
        body += text(48, 69, "KUBERNETES WORKLOAD", 14, gray, 700)
        body += (
            '<rect x="62" y="99" width="295" height="161" rx="8" fill="#f0eaf6" stroke="#71509c"/>'
        )
        body += text(82, 130, "OpenShell sandbox", 17, purple, 700)
        body += text(82, 158, "Read approved files", 15) + text(
            82, 184, "Write bounded scratch space", 15
        )
        body += text(82, 233, "Agent task", 20, purple, 700)
        body += f'<path d="M239 223H553" stroke="{purple}" stroke-width="3"/>'
        body += text(365, 210, "Approved route", 14, purple)
        body += (
            f'<circle cx="593" cy="223" r="40" fill="#f0eaf6" stroke="{purple}" stroke-width="2"/>'
        )
        body += text(548, 153, "AgentGateway", 17, purple, 700)
        body += text(558, 217, "Identity", 14) + text(558, 238, "+ tool", 14)
        for y, label, sub in [
            (82, "Model service", "Approved model + limits"),
            (223, "Read services", "Resource authorization"),
            (364, "Action receiver", "Approval + idempotency"),
        ]:
            body += f'<path d="M633 223C690 223 660 {y} 710 {y}" fill="none" stroke="{purple}" stroke-width="2"/>'
            body += text(718, y - 7, label, 17, purple, 700) + text(718, y + 18, sub, 14)
        body += text(48, 299, "Tetragon / eBPF on the node", 17, orange, 700)
        body += text(48, 323, "Selected kernel evidence + tested responses", 15)
        body += f'<path d="M280 270V289" stroke="{orange}" stroke-dasharray="3 3"/>'
        body += text(
            28,
            396,
            "Temporal coordinates progress and approval waits; it is not an access-control boundary.",
            15,
            gray,
        )
        caption = "Solid lines show supported requests. Sandbox and network controls block direct routes around the gateway; receivers authorize business effects."
    else:
        raise ValueError(f"Unknown deployment diagram: {key}")
    return (
        f'<figure class="deployment-diagram"><p class="diagram-title">{title}</p>'
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 930 {height}" role="img" aria-label="{title}">{body}</svg>'
        f"<figcaption>{caption}</figcaption></figure>"
    )
