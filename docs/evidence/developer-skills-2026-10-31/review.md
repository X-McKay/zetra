# Independent developer skill pack review

Reviewed canonical template change `7c447ac..f598431` in
`/tmp/zetra-template-developer-skills`, the uncommitted SDK delivery against
`158a170` in `/tmp/zetra-developer-skills`, and the design/implementation plan.
Read-only review; only disposable test/build outputs and this report were written.

## Material finding

**[P2] Document the effects admitted by the unchanged starter SDK pin.**

Locations:

- `template/docs/development.md:53` in the canonical template.
- `template/.agents/skills/zetra-author-tool/SKILL.md:11-13`, and its Claude mirror.
- Corresponding files under SDK `src/zetra/templates/agent/`.

The shared guide says the pinned SDK admits `remote_read`; the author-tool skill
offers it as the supported effect for a deployment-history integration. However,
the starter retains SDK revision `af2e7542b89265eaa4572a586719a67e58238e74`, whose
`src/zetra/agent.py:15` accepts only `workspace_read` and `workspace_write`.
Current SDK HEAD supports another effect, but it is not the version installed by
this generated project. Following this guidance therefore fails tool construction
before evaluation and misstates the admitted integration boundary.

Reproduced using the already installed pinned environment:

```python
from zetra.agent import Tool

async def read_history():
    return "synthetic history"

Tool(read_history, "remote_read")
# ValueError: unsupported tool effect
```

Command interpreter: `/tmp/zetra-skills-rendered/.venv/bin/python`. Confirmed the
import came from that environment's installed `site-packages/zetra/agent.py` and
independently checked the source at the exact Git pin.

Recommendation: list only the two admitted effects and identify remote reads as
requiring a separately qualified SDK upgrade/integration. Update both tool-skill
copies through canonical sync and regenerate the inventory and SDK bundle. Keep
the SDK pin/lock unchanged for this delivery, as specified. The implementer has
been notified and plans this correction; this report records the reviewed version.

No additional material findings were identified.

## Verification and deliberate review checks

- Canonical repository suite: **13 passed**.
- Canonical template developer-sync and negative wheel tests: **19 passed**.
- SDK scaffold suite: **18 passed**.
- Existing renamed generated project with its installed pinned SDK: **23 passed**.
- SDK `scripts/sync_template.py /tmp/zetra-template-developer-skills --check`:
  bundle, renderer, and provenance matched the committed canonical tree.
- Additional disposable two-skill conflict case: a later local Claude edit prevents
  an earlier queued canonical update from writing anything; all bytes survived.
- Existing traversal/root-symlink tests passed. Additional managed-leaf symlink
  case rejected before queued writes and preserved the outside file. Inspected
  ancestor and nested-entry validation; no path escape was found for static trees.
- Both the standalone canonical renderer and SDK scaffold retained seven canonical
  and seven Claude skills in renamed projects. Their pack checks passed, and every
  skill's local Markdown link resolved to an existing shared guide.
- Built an SDK wheel offline using cached Hatch tooling: all fourteen hidden skill
  files and the synchronizer were present.
- Built a disposable application wheel with an added packaged runtime `SKILL.md`:
  runtime resource remained present, developer pack remained excluded, and the
  wheel checker accepted it. No unexpected runtime exclusion was observed.
- Inspected all seven skills, shared guide, scenario inputs, CI/Justfile wiring,
  package checks, and pinned fixture implementation. Fixture results are explicitly
  bounded to deterministic host execution; guidance does not promote fixture
  success into model-quality, isolation, or production authorization.
- Synchronization preflights inventoried paths, extra files in managed names, and
  mirror conflicts. Unknown skill names remain project-owned. Inventory/hash
  checking is explicitly described as consistency checking rather than a trust
  boundary; no silent drift affecting the delivered pack was observed.

No network, cluster, credentials, deployment, or paid model calls were used.
Behavioral efficacy across Claude Code/Codex and production enforcement were not
qualified by this review.
