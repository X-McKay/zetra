# Import: the catalog feed and the agents of the pull request path

Milestone 1. Implemented by `agent_registry/importer/` (package 1.2). Every function here is
idempotent: a second call with the same input writes nothing and records no event.

Records are built with the functions in `agent_registry/models.py` (identifiers),
`agent_registry/canonical.py` (documents, digests) and `agent_registry/derive.py` (release
records, requirements, resolutions). The importer does not build these any other way.

## 1. The feed

One JSON file, rendered by Kubani from its own files. Example: `tests/fixtures/feed.json`.

| Key | Entries |
| --- | --- |
| `version` | `1`. Any other value is refused. |
| `clusters` | `name`, `trustDomain`, `gateway`, `fluxInventories` (list of `{namespace, name}`) |
| `tenants` | `cluster`, `name`, `namespace`, `policy` (the tenant's signer pin and exceptions) |
| `trustedRepositories` | `repository`, `cluster`, `tenant`, `issuer`, `subjectRegExp`, `agents` |
| `toolServers` | `name`, `cluster`, `namespace`, `owner`, `description`, `source`, `workload` (`apiVersion`, `kind`, `name`), `image`, `pinnedByDigest`, `registered`, `backend` (null when not registered) |
| `modelEngines` | `name`, `cluster`, `namespace`, `owner`, `description`, `source`, `workload`, `servedName`, `servedNameFrom` (`kind`, `name`, `key`), `backend` (null when the gateway does not route to it) |
| `aliases` | `cluster`, `alias`, `engine` (a model engine's `name`), `backend` |

`load_feed(path)` reads the file and checks it: the version; that every entry has its keys;
that every `cluster` named exists in `clusters`, every `tenant` in `tenants`, every `engine`
in `modelEngines`. A feed that fails a check raises `ValueError` and nothing is written.

## 2. `apply_feed(store, feed, *, now=None, actor="feed") -> bool`

One transaction. Each record is written only when it differs from what is stored. When
anything was written, one event `feed-applied` is recorded, with `detail.changed` listing what
changed (for example `"tenant:kubani/tenant-test"`, `"tool-server:kubernetes-mcp"`). Returns
whether anything changed.

| Feed entry | Records |
| --- | --- |
| cluster | `Cluster` |
| tenant | `Tenant`, with `policy_revision = canonical.digest(<the whole entry>)` and `document = <the entry>`. A change to the tenant's policy therefore changes the policy revision. |
| trusted repository | `TrustedRepository` |
| tool server | `Item` (kind `tool-server`). `Deployment` with id `tool-server:<cluster>:-:<name>`, no tenant, authority `pull-request`, and `document = {namespace, workload, image, pinnedByDigest, registered, backend}`. One adopted `Release` for the declared image (below). No revisions. |
| model engine | `Item` (kind `model`). `Deployment` with id `model:<cluster>:-:<name>` and `document = {namespace, workload, servedName, servedNameFrom, backend}`. One adopted `Release` for the served name. No revisions. |
| alias | `Alias` with `tenant = None` and `deployment_id` of the engine's deployment |

Adopted releases (written once; a new image or served name adds a release and keeps the old):

- Tool server: `name` is `release_slug(<tag>)`, where the tag is the text after the last `:`
  of the image once any `@sha256:...` is removed, provided that `:` comes after the last `/`.
  With no tag, `name` is `release_slug(<the digest>)`, for example `sha256-dbbd3468...`.
  `digest = derive.image_digest(image)`, `origin = "adopted"`, `document = {"image": image}`,
  `lineage = {"source": <source>}`. In the fixture the five names are `v2.0.2`,
  `1.1.1-standalone`, `v0.0.67`, `sha256-dbbd346860d29f1543e991f30f3284bf4ab5f096d049ecc3426528f20b1b6e6b`
  and `0.3.0`.
- Model engine: `name` is `release_slug(servedName)`, `digest = ""`, `origin = "adopted"`,
  `document = {"servedName": servedName}`.

Nothing is deleted. An item of kind `tool-server` or `model` that is no longer in the feed is
marked `retired: true`; if it returns, the mark is cleared. Its deployment and releases stay.

## 3. `import_documents(store, documents, *, cluster, now, actor="importer") -> ImportReport`

`documents` are `Agent` and `AgentRelease` documents (`apiVersion: agentplatform.io/v1alpha1`),
from files (`load_directory`) or from observations. Other kinds are ignored by
`load_directory`. A document may carry `status`; documents from files do not.

**Tenant.** A document belongs to the tenant of `cluster` whose `namespace` equals the
document's `metadata.namespace`. A document in a namespace no tenant has is listed in
`problems` and not imported.

**Per agent**, in one transaction, with one event `agent-imported` (deployment id set,
`detail = {created: [...], updated: [...]}`) only when something was written:

1. If the agent's deployment exists with authority `registry`, nothing is written and the
   deployment id is listed in `skipped`. The registry's own records are the truth for it.
2. `Item` (kind `agent`): `owner` and `description` from the Agent's spec; `source` is the
   `repository` of the trusted repository of this cluster and tenant that lists the agent, or
   `""`.
3. `Deployment`: id `agent:<cluster>:<tenant>:<name>`, authority `pull-request`,
   `enabled = spec.enabled` (true when absent),
   `document = {"agent": canonical.agent_document(agent)}`. An existing deployment keeps its
   `pinned_revision`, `current_revision` and `generation`; its `enabled` and `document` follow
   the Agent document. `canonical.agent_document` fills in the resource's defaults, so a
   manifest read from git and the same Agent read from the cluster give the same document,
   and importing one after the other changes nothing.
4. Releases of this agent (`spec.agent` equal to the agent's name, same namespace) that are
   not yet a revision, in this order: approved ones first, by `approval.approvedAt` and then
   name; then unapproved ones by name. For each:
   - `Release` from `derive.agent_release_record`, with its requirements
     (`derive.requirements_of(release id, agent document)`). If a release with this id is
     already stored with a different document, the release is listed in `problems` and
     skipped.
   - `Revision` with the next number: origin `imported`, the Agent document, its authority
     and authority digest (`canonical.authority_of`, `canonical.authority_digest`), the
     tenant's `policy_revision`, `generation = 0`. Approved: phase `activated`, `activated_at`
     the approval time. Not approved: phase `awaiting-runtime`, reason `imported unapproved`.
   - Resolutions from `derive.resolutions_of`.
   - Approved: an `Approval` with id `imported:<deployment id>@<number>`, origin `imported`,
     `approved_by`, `approved_at` and `ticket` from the block, `authority_digest` of the
     revision, `base_authority_digest = ""`, the tenant's `policy_revision`, and
     `evidence = {"approval": <the spec.approval block exactly as written>}`. The revision's
     `approval_id` is set to it.
5. A release that is already a revision without an approval, and whose document now carries
   `approval.approved: true`: the approval is added as above and the revision becomes
   `activated`.
6. `current_revision`: the highest-numbered revision that has an approval. When the
   documents carry `status`, only revisions whose AgentRelease has the `Ready` condition
   `True` count; a revision whose release is not among the documents does not count. When
   none qualifies the pointer keeps its value. A pinned deployment keeps its pointer.

**Orphans.** An AgentRelease whose agent has no Agent document in the same namespace is
listed in `orphans` as `<namespace>/<name>` and is not stored.

**Removed documents.** An agent or release that is no longer in the documents is left as it
is. Nothing is deleted.

`ImportReport`: `agents` and `releases` count the documents read; `created` lists the ids of
records written for the first time (items as `agent:<name>`, deployments, releases, revisions,
approvals); `updated` lists the ids of deployments or items whose record changed;
`orphans`, `skipped`, `problems` as above. `changed` is true when `created` or `updated` is
not empty.

With the fixtures (`tests/fixtures/manifests`, `tests/fixtures/feed.json`): 13 agents,
23 releases, 13 deployments, 23 revisions, 23 approvals, no orphan, no problem.
`db-admin` has two revisions: 1 is `db-admin-2026-11-07-3e94dc5e`, 2 is
`db-admin-2026-11-13-33c8acb2`, and the pointer is 2. Its `postgres-admin` requirement
resolves to `tool-server:kubani:-:postgres-admin-mcp`, and its `llm-main` requirement to
`model:kubani:-:main` through the alias `main`. The model backend `llm-budget-conformance`
resolves to nothing.

## 4. `index_observations(store, cluster, *, now, actor="indexer") -> ImportReport`

The same as section 3, with the documents taken from the cluster's stored observations of
kind `agent` and `agentrelease` that are present. Each document is the observation's
`document` (`metadata`, `spec`, `status`). It runs after every observation report, so the
index follows what pull requests change without anyone running an import.

## 5. `agent-registry import --dir DIR --feed FILE [--cluster NAME]`

Applies the feed, then imports the documents under `DIR`. Prints one JSON document: the
report's fields and `feedChanged`. Exit status 0; 1 when `problems` is not empty.
