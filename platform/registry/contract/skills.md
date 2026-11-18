# Skills, with scripts

Milestone 4. An agent may carry skills in the Agent Skills format. A skill may bring a
script, and the platform decides whether a deployment may run it.

Where this document and `delivery.md` or `dependencies.md` disagree about new behaviour, this
one is right. Everything they say that is not changed here still holds.

Cases as data: `skill-digest-cases.json` (section 3) and `effects.json` (section 7). The
reference implementation of
sections 2 to 5 is `platform/agent-runtime/agent_runtime/skills/format.py`; every other
package imports it and none writes a second one.

## 1. What is in, and what waits

In: skills kept in the agent's repository; a list of skills per agent; a lock that pins each
to a digest; a sidecar file that states what a skill needs; a build that copies the pinned
skills into the image; discovery and activation at run time; one effect, `sandbox_execute`,
that a release requests and a tenant grants; a launcher that runs a named script under six
controls; a record of every run; and skills in the catalog, taken from what agent releases
carry.

Waits, on purpose:

- A sandbox apart from the agent's container. Scripts run in the agent's container (section
  9.5 says what that means for the identity token).
- Skills adopted from elsewhere, sharing a skill between repositories, and a packed artifact
  for one skill. Until then a skill name is unique in a registry.
- Signing a skill, and any attestation about one.
- Running a skill's evaluation cases. The sidecar records where they are.
- The grant in the feed's tenant policy. The grant is read on the cluster (section 7.2).
- "Granted, but marked for review" as its own state. Every release that newly asks for the
  effect needs an approval that shows it (section 7.4), which is the review.
- A rule that a changed skill must carry a new `metadata.version`.
- A kernel limit on a script's memory and process count. Section 9.4 says what is enforced
  by sampling.

The code is in `platform/agent-runtime` (`agent_runtime`), which is what platform agents run
on. The older harness in `src/zetra/` is not changed and has no skills.

## 2. A skill and where it lives

A skill is a directory in the Agent Skills format: a `SKILL.md` with front matter (`name`,
`description`, optional `license`, `compatibility`, `metadata`, `allowed-tools`) and a body,
plus any files it refers to. The format is not extended. `SKILL.md` gets no new key.

- The directory name is the skill name: lowercase letters, digits and single hyphens, at
  most 64 characters, equal to `name` in the front matter.
- `description` is required, at most 1024 characters.
- The version is `metadata.version`, read as text (`1.10` stays `1.10`), one word of at most
  64 characters. It may be absent.
- `allowed-tools` is never authority. `check` reports that it is present and ignores it.
- A skill holds regular files and directories only. A symbolic link anywhere in it is
  refused.

In an agent repository skills are at the root, in `skills/<skill-name>/`, so that two agents
can use one. An agent lists the skills it uses in `agent.yaml`:

```yaml
name: db-admin
skills:
  - row-count
```

- `skills` is a top-level list of skill names, each a directory under `skills/`, no name
  twice, at most 16. An absent or empty list means no skills, and then nothing in this
  document applies to the agent.
- Listing skills needs the one-file layout (`delivery.md` section 1.1). An agent without a
  `requires` block that lists skills fails `check`.

## 3. The digest of a skill

The digest names the content of a skill directory.

1. Take every regular file at any depth, except files under a directory named
   `__pycache__`.
2. For each write one line: the lowercase hex SHA-256 of its content, a space, `x` when the
   file's owner may execute it and `-` when not, a space, the path relative to the skill
   directory with `/` between parts, and a line feed.
3. Sort the lines by the bytes of the path (UTF-8).
4. The digest is `sha256:` and the lowercase hex SHA-256 of the lines joined together.

Refused, with no digest: a symbolic link, a file that is neither regular nor a directory, a
path that contains a line break, and a skill directory that is itself a link.

Only the owner's execute bit counts, so the digest is the same in a checkout, in the build
and in the image. Empty directories do not count.

`skill-digest-cases.json` gives five directories with the lines and the digest of each.

## 4. The lock

`agents/<agent>/skills.lock` pins every listed skill:

```json
{
  "schema_version": 1,
  "skills": [
    {
      "digest": "sha256:62a3db36393acf72fa07c67ca68a62bec1952926d511c65f5052dc5c3c2a89d0",
      "name": "row-count",
      "version": "1.2"
    }
  ]
}
```

- Entries are sorted by name. `version` is left out when the skill states none. The text is
  JSON with sorted keys, two spaces of indent and a final line feed.
- The file is absent when the agent lists no skills.
- `zetra-agents render` writes it. `render --check`, `check` and the image build fail when
  it is stale: a listed skill that is not pinned, a pinned skill that is not listed, or a
  digest or version that differs from the directory. The message names the skill and the
  command that writes the lock.
- The lock is under `agents/<agent>/`, so a changed skill changes a file of every agent that
  uses it, and the existing rule that a changed agent needs a new version applies.

Two agents that list the same skill pin the same digest, because the digest is of one
directory.

## 5. The sidecar

A skill may have `zetra.yaml` beside `SKILL.md`. It states what the platform needs to know
and nothing the model reads.

```yaml
schema_version: 1
requires:
  tools:
    - backend: postgres-admin
      tools: [execute_sql]
scripts:
  - path: scripts/count.py
    interpreter: python3
    timeout_seconds: 30
evaluation: evals/cases.yaml
```

| Key | Meaning |
| --- | --- |
| `schema_version` | `1`. Required. |
| `requires.tools` | Tool backends and the tools on each that the skill's instructions use. No backend twice. |
| `scripts` | The scripts that may be run. `path` is a file of the skill under `scripts/`, in letters, digits, `.`, `-`, `_` and `/`, at most 128 characters. `interpreter` is a bare command name such as `python3` or `bash`: no path, no arguments. `timeout_seconds` is 1 to 240, default 60. No path twice. |
| `evaluation` | A file of the skill that holds its evaluation cases. Recorded, not run. |

An unknown key anywhere fails. A sidecar that says only `schema_version: 1` is valid: the
skill declares that it needs nothing.

A skill with **no** sidecar is valid. Its requirements are **undeclared**: `check` reports
that its tool needs are unknown, and none of its files can be run, whatever the directory
holds. A file that is not listed under `scripts` can never be run.

A script is run by the interpreter the sidecar names, found in the image. It is not run
through a shell, a `#!` line is not read, and it gets the image's interpreter, not the
agent's virtual environment.

## 6. What `check` holds

For an agent that lists skills, `zetra-agents check` fails unless all of these hold. Each
failure is one line that starts with the agent's name, as today.

1. Each listed skill is a directory under `skills/` that `skills-ref validate` accepts
   (`skills-ref==0.1.1`, a dependency of the tooling only; it is never in an image) and that
   section 2 accepts.
2. The sidecar, when present, is as section 5 says.
3. The lock pins exactly the listed skills as they are (section 4).
4. **Tools are covered.** For every backend in a skill's `requires.tools`, the agent's
   `requires.tools` has that backend with at least those tools. Requirements are never
   merged in silently. The failure prints the lines to add:

   ```text
   db-admin: skill row-count needs tools the agent does not request; add to agent.yaml:
     requires:
       tools:
         - backend: postgres-admin
           tools: [execute_sql]
   ```

5. **The effect is requested exactly when it is needed.** If a listed skill declares
   scripts, the agent requests `sandbox_execute` (section 7.1); the failure prints the lines
   to add. If the agent requests it and no listed skill declares scripts, `check` fails and
   says to remove it.
6. Nothing in the agent's `src/` is named `skills` or `skills.lock` (section 8.1).

`check` also prints, without failing: each skill with no sidecar, as
`<agent>: skill <name> has no zetra.yaml: its tool needs are unknown and its scripts do not
run`; and each skill whose front matter has `allowed-tools`, as ignored.

That a named interpreter exists is checked where it can be known, in the image build
(section 12).

## 7. The effect `sandbox_execute`

### 7.1 The request

The effect vocabulary gains a fourth word, `sandbox_execute`. The two lists
(`agent_runtime.tools.EFFECTS` and `zetra_agents.layout.EFFECTS`) stay equal and the test
that holds them equal stays.

A tool the author writes still has one of the first three effects. `sandbox_execute` on a
tool in the manifest's `tools` list is refused by `check` and by the runtime: the effect
belongs to the built-in launcher (section 8.3) and to nothing else.

The author requests it in the one file:

```yaml
requires:
  effects: [sandbox_execute]
```

`requires.effects` is a list whose only admitted member is `sandbox_execute`. It generates
`spec.capabilities.effects: [sandbox_execute]` in the Agent, and the release carries it in
`spec.declaration` (`delivery.md` section 2). When nothing is requested the key is **absent**
from the generated Agent, not an empty list. Both resource definitions and both copies of
the Agent schema describe the field.

An agent on the two-file layout could write `spec.capabilities.effects` in its Agent by
hand. It cannot list skills (section 2), so rule 5 of section 6 refuses the request there.

### 7.2 The grant

A tenant grants effects on its Namespace:

```yaml
metadata:
  annotations:
    agentplatform.io/granted-effects: sandbox_execute
```

The value is a comma-separated list of effect names; spaces around a name are ignored. No
annotation grants nothing. The Namespace is the cluster repository's, so a grant is a
reviewed change there.

One function decides, and every program that needs the answer calls it:

```python
# agent_platform/effects.py
ANNOTATION_GRANTED_EFFECTS = "agentplatform.io/granted-effects"
def requested(agent_spec: dict) -> list[str]          # sorted, from capabilities.effects
def granted(namespace: dict | None) -> set[str]       # from the annotation
def ungranted(agent_spec: dict, namespace: dict | None) -> list[str]
def statement(effect: str) -> str                     # the line of section 7.4
```

An effect the code does not know is never granted, whatever the annotation says.

The Registry Controller reads the Namespace it already reads for the signer identity. A
release whose declaration (or, for a release without one, whose Agent) requests an effect
the Namespace does not grant is not Ready:

- `ReleaseFacts` gains `effects_granted`, and `release_ready` requires it.
- The Ready condition's reason is `EffectNotGranted`, and it comes **before**
  `NotApproved`. A release that asks for an ungranted effect is therefore never "not Ready
  solely for want of approval", and the approval commands refuse it.
- The condition's message names the effects that are not granted.
- Withdrawing a grant makes the tenant's releases that request the effect not Ready at the
  next reconcile, and sessions are refused for them as for any release that is not Ready.

The registry adds no gate of its own. It learns the refusal from the controller's verdict,
as it learns every other gate (`rules/gates.py`).

### 7.3 In the compiled policy and the Pod

`compile_bundle` adds `"effects": ["sandbox_execute"]` to the bundle **only when the list is
not empty**. A bundle for an agent that requests no effect is byte for byte what it is
today, so no existing `policySha256` moves. The Agent Policy Service ignores the key.

The pod template gains the environment variable `AGENT_EFFECTS` (comma-separated, sorted)
**only when the bundle has effects**, so no existing template changes.

### 7.4 Authority and what an approval shows

In the registry, `canonical.authority_of` adds `"effects"` (sorted) to the authority **only
when the list is not empty**, so no existing authority digest moves. Asking for an effect
the approved authority does not have widens it. `rules/authority.widens` reports it with
this sentence, and every place that states what an approval covers (the registry's answer
for an approval that is needed, `zetra-agents preflight`, and the cluster repository's
`approve` command for the pull request path) shows the same sentence when the release
requests the effect:

```text
effect sandbox_execute: skill scripts run inside the agent's container and can read the session's identity token
```

The text is in `effects.json`. `agent_platform.effects.STATEMENTS` holds it for the
controller's side and the tooling, and the registry, which does not import that package,
holds its own copy; a test in each holds the copy equal to the file.

### 7.5 What is claimed

The model cannot cause a skill script to run unless the deployment was granted the effect:
no session starts for a release that is not Ready, a release that requests an ungranted
effect is never Ready, and the launcher is offered only where the Pod carries the effect.

Nothing more is claimed. In particular this is not a statement about what the agent's own
code can do in its container.

## 8. In the image and at run time

### 8.1 Where skills are

In the image the pinned skills are at `/app/skills/<name>/` and the lock at
`/app/skills.lock`; in code, `app_dir() / "skills"` and `app_dir() / "skills.lock"`. Both
are read-only there. The local harness (`zetra-agents run`) lays its app directory out the
same way.

When the manifest lists skills, the step refuses to load the agent (an agent error, exit 3)
unless every listed skill is a directory there, the lock pins exactly the listed skills, and
each directory reads as section 2 says. Digests are not recomputed at load. They are
recomputed before a script runs (section 9.2).

Nothing is fetched at run time. Every skill tool reads the image and nothing else.

### 8.2 Discovery

When at least one skill is listed, the system prompt is the agent's instructions followed by:

```text

## Skills

Before a task that one of these skills covers, call `skill_use` with its name and follow what it returns.

- row-count: Counts rows in a table. Use when asked how many rows a table has.
```

One line per skill in the order the manifest lists them: `- <name>: <description>` with the
description's line breaks replaced by spaces. Nothing else of a skill is in a model request
until the model asks for it, so an unused skill costs its description and no more.

### 8.3 Built-in tools

Three tools are provided by the runtime. Their names are reserved: an author's tool with one
of these names is refused by `check` and by the runtime. They are not in the manifest's
`tools` list and they state their own input schemas. A call to one is a tool boundary like
any other, and its result is bounded like any other tool result.

| Tool | Offered when | Input | Effect |
| --- | --- | --- | --- |
| `skill_use` | a skill is listed | `{"skill": name}` | `workspace_read` |
| `skill_file` | a skill is listed | `{"skill": name, "path": text, "offset": integer?}` | `workspace_read` |
| `skill_run` | `AGENT_EFFECTS` has `sandbox_execute` **and** a listed skill declares scripts | `{"skill": name, "script": path, "arguments": [text, ...]}` | `sandbox_execute` |

`skill_use` returns

```json
{"skill": "row-count", "version": "1.2", "body": "...", "files": ["references/notes.md"],
 "scripts": ["scripts/count.py"]}
```

where `body` is `SKILL.md` after its front matter, `files` is every file of the skill except
`SKILL.md` and `zetra.yaml`, sorted, and `scripts` is what `skill_run` would accept for this
skill in this session (empty when the skill is undeclared, declares none, or the launcher is
not offered). `SKILL.md` may be at most 32768 bytes; `check` fails a larger one.

`skill_file` returns `{"skill", "path", "content", "next_offset"}`: up to 24000 characters of
a UTF-8 text file of the skill from character `offset` (default 0), and the offset to ask
for next, or `null` at the end. The path is confined as section 9.2 says.

A refusal is a result, not an error: `{"refused": code, "message": text}` with the codes of
section 9.3, plus `not_text` for a file that is not UTF-8.

`skill_run` takes a skill name, a script name and a list of arguments. It does not take a
command line.

## 9. The launcher

`agent_runtime/skills/launcher.py`. It needs Linux and `/proc`; anywhere else it refuses.

### 9.1 Interface

```python
@dataclass(frozen=True)
class Limits:
    timeout_seconds: int            # from the sidecar
    output_bytes: int = 16384       # per stream
    memory_bytes: int = 536870912   # 512 MiB
    processes: int = 32

@dataclass(frozen=True)
class Run:
    skill: str | None               # None when the name given was not a listed skill
    script: str | None              # None when the name given was not a listed script
    digest: str | None              # of the skill directory as it was run
    arguments: tuple[str, ...]      # in full
    refused: str | None             # a code of 9.3, or None when the script was started
    exit_status: int | None         # None when refused or stopped; negative N: signal N
    limits_hit: tuple[str, ...]     # of "time", "output", "memory", "processes"; sorted
    stdout: str
    stderr: str
    duration_ms: int

def run(app: Path, workspace: Path, listed: Sequence[str],
        skill: object, script: object, arguments: object,
        *, limits: Limits | None = None) -> Run
```

`limits` is for tests; left out, the bounds are the ones here with the script's own timeout.

`run` never raises for anything the model sent. It raises `LauncherFailure` when it cannot
say that the script's processes are gone (section 9.4); the step then exits with status 6,
which is not one of the statuses the runner knows (0, 2, 3, 4 and 5), and the runner
replaces the sandbox.

### 9.2 What is run, and where

1. **Explicit arguments.** The process is the interpreter, the script's path and the
   arguments, as a list. There is no shell and no interpretation of any argument. Arguments
   are a list of at most 32 strings, each at most 4096 bytes, at most 16384 bytes together,
   none with a NUL character.
2. **Path confinement.** The script must be listed in the skill's sidecar. Its real path
   must equal `app/skills/<skill>/<path>` with no link at any step, and it must be a regular
   file. The working directory is the workspace, which must be a real directory. The same
   rule confines `skill_file` to the skill's directory.
3. Before the script starts, the skill's digest is computed and must equal the lock's. The
   digest in the record is therefore of what ran.
4. The interpreter is found on `/usr/local/bin:/usr/bin:/bin` and nowhere else.
5. The script's environment is exactly `PATH` (those three directories), `HOME` (the
   workspace), `LANG=C.UTF-8` and `PYTHONDONTWRITEBYTECODE=1`. Standard input is empty. This
   keeps the agent's variables out of the script. It is not a boundary (section 9.5).

### 9.3 Refusals

| Code | When |
| --- | --- |
| `unknown_skill` | The name is not a skill the agent lists. |
| `undeclared` | The skill has no sidecar. |
| `unknown_script` | The script is not listed in the sidecar. |
| `path` | A link, a file that is not regular, or a path that leaves the skill directory. |
| `arguments` | Not a list of strings within the bounds. |
| `interpreter` | The interpreter is not found. |
| `digest` | The skill directory is not what the lock pins. |
| `workspace` | The workspace is not a real directory. |
| `unsupported` | Not Linux, or no `/proc`. |

A refused run starts no process.

### 9.4 Supervision, limits and the end of a run

The script does not run as a child of the step. The step starts a supervisor
(`python -m agent_runtime.skills.supervisor`, the agent's own interpreter), which starts the
script and does nothing else. The supervisor:

- makes itself the reaper of every process the script leaves behind
  (`PR_SET_CHILD_SUBREAPER`), so a process that detaches is still its descendant;
- asks to be signalled when the step dies (`PR_SET_PDEATHSIG`), and treats that, `SIGTERM`,
  `SIGINT` and `SIGHUP` as the end of the run;
- starts the script in a new session and reads its two output streams.

**The tree is ended** on every way out: the script exiting, a limit, a signal, the step
dying. Ending means: find every descendant through `/proc`, stop each, repeat until a pass
finds none that is not stopped, kill them all, reap, and repeat until none is left. A
process a script leaves in the background is killed when the script exits: nothing of a run
outlives it. If a descendant remains after 5 seconds, or the supervisor dies without
answering, the launcher raises `LauncherFailure`.

Ending the session deletes the sandbox Pod, which ends every process in it. The launcher's
part is that ending a boundary, or the step, ends the script's whole tree.

| Limit | Bound | How it is enforced | Reported as |
| --- | --- | --- | --- |
| Time | `timeout_seconds` of the script (1 to 240) | A deadline on a monotonic clock | `time` |
| Output | 16384 bytes on each stream | Counted as read; the run stops at the first byte over | `output` |
| Memory | 512 MiB of anonymous and shared memory, summed over the tree | Sampled from `/proc` about every 20 ms | `memory` |
| Processes | 32 tasks (threads count) in the tree | Sampled with memory | `processes` |

A limit stops the run: the tree is ended, `exit_status` is `null` and `limits_hit` names the
limit. Output read before the stop is returned, cut to the bound. Output is decoded as
UTF-8 with replacement.

Memory and process count are sampled, not held by the kernel. A script that allocates or
forks faster than the sampler sees is stopped late, and until then only the container's
memory limit and the node's process limit for the Pod hold it. The longest script run (240
seconds) is under the runner's boundary deadline.

The boundary may be run again on a replaced sandbox from the last committed snapshot
(`runner/workflow.py`), so a script may run more than once for one tool call. A script must
be safe to run twice.

### 9.5 Credential isolation

Scripts run inside the agent's container, as the agent's user, on the agent's network. A
script can read the session's identity token and can call whatever the agent may call.
Clearing the environment (9.2) does not change that. This is why the effect is granted per
tenant and why an approval states it (section 7.4). Running scripts where the token is not
readable waits (section 1).

## 10. The record of a run

Every call of `skill_run`, refused or not, is recorded.

The step's output for that boundary gains one key, `script_run`, present on a `skill_run`
boundary and on no other:

```json
{"boundary": 3, "kind": "tool", "done": false, "tool": "skill_run", "usage": null,
 "summary": "...",
 "script_run": {
   "skill": "row-count", "script": "scripts/count.py",
   "digest": "sha256:62a3db36393acf72fa07c67ca68a62bec1952926d511c65f5052dc5c3c2a89d0",
   "argument_count": 2,
   "arguments_sha256": "5d1c...",
   "exit_status": 0, "limits_hit": [], "refused": null, "duration_ms": 41}}
```

- `skill` and `script` are `null` when the name the model gave was not a listed one, so the
  record never carries text the model made up. `digest` is `null` when there is no skill.
- `arguments_sha256` is the hex SHA-256 of the arguments as compact JSON
  (`json.dumps(list, ensure_ascii=False, separators=(",", ":"))`, UTF-8). The arguments
  themselves are task content. They stay in the session's transcript, in the tool call the
  model made, and the launcher returns them in full to its caller. The audit record carries
  their count and hash, as it carries the hash of a boundary's summary.

The runner accepts `script_run` as an optional key of the boundary output and validates it
strictly: exactly these nine keys; `skill` a skill name or null; `script` a script path of
section 5 or null; `digest` a digest or null; `argument_count` 0 to 32; `arguments_sha256`
64 hex characters; `exit_status` an integer from -64 to 255 or null; `limits_hit` a sorted
list without repeats of the four limit names; `refused` a code of section 9.3 or null;
`duration_ms` a non-negative integer. `script_run` on a boundary whose `tool` is not
`skill_run`, or a `skill_run` boundary without it, is a protocol error.

It then emits one audit record, `script_run`, with `session_id`, `generation`, `boundary`
and the nine keys. The local harness reads the key the same way and prints the record.

An older runner rejects the new key. The runner is therefore rolled out before any image
that can run a script.

## 11. The release and the catalog

### 11.1 The release

`zetra-agents release` adds `spec.skills` to the AgentRelease when the agent lists skills,
one entry per skill in name order, from `format.release_entry`:

```yaml
spec:
  skills:
    - name: row-count
      version: "1.2"
      digest: sha256:62a3db36393acf72fa07c67ca68a62bec1952926d511c65f5052dc5c3c2a89d0
      description: Counts rows in a table. Use when asked how many rows a table has.
      declared: true
      scripts: [scripts/count.py]
      interpreters: [python3]
      tools:
        - backend: postgres-admin
          tools: [execute_sql]
```

`version`, `scripts`, `interpreters` and `tools` are left out when there is nothing to say.
The key is absent for an agent with no skills. The resource definition describes the field
(at most 16 entries). It is the pipeline's statement of what the image holds; the image
digest covers the content. Neither the controller nor the policy reads it. The registry
keeps it in the release's document, so a release it writes to a cluster carries it
unchanged.

### 11.2 The catalog

`agent_registry/importer/skills.py` runs wherever an agent release is recorded (the release
report, the operator's route and the cluster import). For each entry of `spec.skills`:

- an item `(kind "skill", name)`, created when first seen with the agent's owner, the
  release's source and the entry's description; later releases update the description and
  nothing else;
- a release of that item named `sha256-<64 hex>`, with the entry's digest and version,
  origin `built`, the agent release's lineage, and the entry as its document. A digest
  already recorded for the item adds nothing.

`derive.requirements_of` adds to the agent release: one requirement of type `skill` per
entry (target the skill name, names the one digest) and one of type `effect` per requested
effect (target the effect name).

`GET /v1/dependants` accepts `type=skill` and answers, as for a tool, the agents whose
release in force requires the skill. The item view of a skill lists its releases.

Skills reach the registry only inside agent release reports, which the registry already
accepts only from trusted repositories. No new route writes a skill.

## 12. The build

The image build, from the repository's one `Dockerfile`:

- copies only the pinned skills, to `/app/skills/`, and the lock to `/app/skills.lock`;
- fails when the lock is stale (section 4), by running the reference implementation in the
  build stage: `python -m agent_runtime.skills.image stage --agent agents/<name> --skills
  skills --out <dir>` writes `<dir>/skills/<name>/` for each listed skill and
  `<dir>/skills.lock`, or prints one line per problem and exits 1. For an agent with no
  skills it writes an empty `<dir>/skills/` and no lock, so the runtime stage copies `<dir>`
  into `/app` whether or not there are skills;
- fails when an interpreter a pinned skill names is not found in the runtime image, by
  running `python -m agent_runtime.skills.image verify --app /app` in the runtime stage,
  which also recomputes each digest there and fails on a directory under `/app/skills/` the
  agent does not list;
- works unchanged for an agent with no skills and for a repository with no `skills/`
  directory.

`dockerignore` lets `skills/**` and `agents/*/skills.lock` into the build context.

## 13. Which package changes what

| Package | Files it owns | Sections |
| --- | --- | --- |
| 4.0 Contracts | this file, `skill-digest-cases.json`, `effects.json`, `agent_runtime/skills/format.py`, `agent_runtime/skills/image.py`, the interface in `agent_runtime/skills/launcher.py`, the effect lists and reserved names in `agent_runtime/tools.py` and `zetra_agents/layout.py`, `requires.effects` in `zetra_agents/declaration.py`, `deploy/platform/crds/` and the two copies of the Agent schema, `agent_platform/effects.py` | 2 to 5, 7.1, 7.2 (the function), 9.1, 11.1 (the resource definition), 12 (`image.py`) |
| 4.1 Discovery | `agent_runtime/skills/tools.py`, `agent_runtime/step.py` (loading, the prompt, the built-in tools, the `script_run` key) | 8, 10 (the step's side) |
| 4.2 Effect | `platform/contracts/agent_platform/` (`bundle.py`, `podtemplate.py`), `platform/controller/` | 7.2 (the controller), 7.3 |
| 4.3 Launcher | the body of `agent_runtime/skills/launcher.py`, `agent_runtime/skills/supervisor.py` and their tests | 9 |
| 4.4, 4.5 Tooling | `zetra_agents/skills.py`, the hooks in `layout.py`, `render.py`, `release.py`, `local.py`, `testing.py` and `cli.py`, `templates/Dockerfile.tmpl`, `templates/dockerignore.tmpl`, `pyproject.toml` and its lock | 4, 6, 8.1 (the local harness), 10 (the local harness), 11.1, 12 |
| 4.6 Catalog | `agent_registry/importer/skills.py`, `canonical.py`, `derive.py`, `rules/authority.py`, the `type=skill` answer of `/v1/dependants`, `contract/openapi.json` | 7.4, 11.2 |
| Runner record | `runner/types.py`, `runner/records.py`, the activity that emits records | 10 (the runner's side) |
| 4.7 Example and loop test | `platform/looptest/`, the example skill in the agents repository, the cluster repository | all, end to end |

4.1 and 4.3 meet at `launcher.run` (section 9.1) and at nothing else: 4.1 calls it, turns a
`Run` into the tool result and the `script_run` key, and does not start a process itself.
4.4 adds one call to `skills.problems(agent, manifest)` in `layout.py`'s `check` and
changes nothing else there. It reads the generated Agent and does not change the generator.

Order on a cluster: the resource definitions, then the controller, the registry and the
runner, then the tenant's grant, then any release that lists a skill with a script.
