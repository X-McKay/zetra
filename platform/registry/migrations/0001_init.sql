-- Milestone 1. One table per record class of agent_registry/models.py; column names are the
-- field names. Documents are jsonb. Nothing here is ever deleted by the service.
--
-- The migration runner keeps its own table (schema_migrations) and runs each file in one
-- transaction. Files are applied in name order and never edited after they are merged.

CREATE TABLE meta (
    only_row        boolean PRIMARY KEY DEFAULT true CHECK (only_row),
    mode            text NOT NULL DEFAULT 'normal' CHECK (mode IN ('normal', 'recovery')),
    mode_reason     text NOT NULL DEFAULT '',
    mode_changed_at timestamptz,
    -- The highest event sequence number handed out. A write transaction locks this row first
    -- (SELECT ... FOR UPDATE), which is what serialises writers, and takes numbers from it, so
    -- a transaction that rolls back uses none.
    seq             bigint NOT NULL DEFAULT 0
);
INSERT INTO meta DEFAULT VALUES;

CREATE TABLE clusters (
    name             text PRIMARY KEY,
    trust_domain     text NOT NULL,
    gateway          text NOT NULL,
    flux_inventories jsonb NOT NULL DEFAULT '[]'
);

CREATE TABLE tenants (
    cluster         text NOT NULL REFERENCES clusters (name),
    name            text NOT NULL,
    namespace       text NOT NULL,
    policy_revision text NOT NULL,
    document        jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (cluster, name)
);

CREATE TABLE trusted_repositories (
    repository      text PRIMARY KEY,
    cluster         text NOT NULL,
    tenant          text NOT NULL,
    issuer          text NOT NULL,
    subject_reg_exp text NOT NULL,
    agents          jsonb NOT NULL DEFAULT '[]'
);

CREATE TABLE items (
    kind        text NOT NULL CHECK (kind IN ('agent', 'tool-server', 'skill', 'model')),
    name        text NOT NULL,
    owner       text NOT NULL DEFAULT '',
    source      text NOT NULL DEFAULT '',
    description text NOT NULL DEFAULT '',
    retired     boolean NOT NULL DEFAULT false,
    PRIMARY KEY (kind, name)
);

CREATE TABLE releases (
    id          text PRIMARY KEY,
    kind        text NOT NULL,
    item        text NOT NULL,
    name        text NOT NULL,
    version     text,
    digest      text NOT NULL,
    origin      text NOT NULL CHECK (origin IN ('built', 'adopted')),
    lineage     jsonb NOT NULL DEFAULT '{}',
    document    jsonb NOT NULL DEFAULT '{}',
    recorded_at timestamptz NOT NULL,
    UNIQUE (kind, item, name),
    FOREIGN KEY (kind, item) REFERENCES items (kind, name)
);

-- position keeps the order the rows were given in; a release's rows are written together, once.
CREATE TABLE release_requirements (
    release_id text NOT NULL REFERENCES releases (id),
    position   integer NOT NULL,
    type       text NOT NULL CHECK (type IN ('tool', 'model', 'effect', 'skill')),
    target     text NOT NULL,
    names      jsonb NOT NULL DEFAULT '[]',
    detail     jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (release_id, position)
);
CREATE INDEX release_requirements_target ON release_requirements (type, target);

CREATE TABLE release_provisions (
    release_id text NOT NULL REFERENCES releases (id),
    position   integer NOT NULL,
    type       text NOT NULL CHECK (type IN ('tool', 'capability')),
    name       text NOT NULL,
    detail     jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (release_id, position)
);

-- A release whose set of requirements or provisions is empty has still had its set written.
-- This table (and revision_resolved below) records that, so that writing a different set
-- later is a conflict and not a first write.
CREATE TABLE release_described (
    release_id   text NOT NULL REFERENCES releases (id),
    what         text NOT NULL CHECK (what IN ('requirements', 'provisions')),
    PRIMARY KEY (release_id, what)
);

CREATE TABLE deployments (
    id               text PRIMARY KEY,
    kind             text NOT NULL,
    item             text NOT NULL,
    cluster          text NOT NULL,
    tenant           text,
    namespace        text NOT NULL,
    authority        text NOT NULL DEFAULT 'pull-request'
                     CHECK (authority IN ('pull-request', 'registry')),
    enabled          boolean NOT NULL DEFAULT true,
    pinned_revision  integer,
    current_revision integer,
    document         jsonb NOT NULL DEFAULT '{}',
    generation       bigint NOT NULL DEFAULT 0,
    FOREIGN KEY (kind, item) REFERENCES items (kind, name)
);
CREATE INDEX deployments_cluster ON deployments (cluster, kind);

CREATE TABLE deployment_revisions (
    deployment_id    text NOT NULL REFERENCES deployments (id),
    number           integer NOT NULL CHECK (number >= 1),
    id               text NOT NULL UNIQUE,
    release_id       text NOT NULL REFERENCES releases (id),
    origin           text NOT NULL CHECK (origin IN ('imported', 'registry', 'recovered')),
    agent_document   jsonb NOT NULL DEFAULT '{}',
    authority        jsonb NOT NULL DEFAULT '{}',
    authority_digest text NOT NULL DEFAULT '',
    policy_revision  text NOT NULL DEFAULT '',
    created_at       timestamptz NOT NULL,
    -- progress: the only columns of this table that change
    phase            text NOT NULL DEFAULT 'awaiting-runtime'
                     CHECK (phase IN ('awaiting-runtime', 'pending-approval', 'activated',
                                      'superseded', 'evaluating', 'refused')),
    approval_id      text,
    reason           text NOT NULL DEFAULT '',
    activated_at     timestamptz,
    generation       bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (deployment_id, number)
);

CREATE TABLE revision_resolutions (
    revision_id         text NOT NULL,
    position            integer NOT NULL,
    type                text NOT NULL,
    target              text NOT NULL,
    resolved_deployment text,
    resolved_release    text,
    detail              jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (revision_id, position)
);

CREATE TABLE revision_resolved (
    revision_id text PRIMARY KEY
);

-- tenant is '' for a cluster-wide alias, so that the key has no null in it.
CREATE TABLE aliases (
    cluster       text NOT NULL,
    tenant        text NOT NULL DEFAULT '',
    alias         text NOT NULL,
    deployment_id text NOT NULL,
    backend       text NOT NULL,
    PRIMARY KEY (cluster, tenant, alias)
);

CREATE TABLE approvals (
    id                    text PRIMARY KEY,
    deployment_id         text NOT NULL,
    revision              integer NOT NULL,
    authority_digest      text NOT NULL,
    base_authority_digest text NOT NULL,
    policy_revision       text NOT NULL,
    approved_by           text NOT NULL,
    approved_at           timestamptz NOT NULL,
    ticket                text,
    origin                text NOT NULL CHECK (origin IN ('imported', 'registry', 'recovered')),
    state                 text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'superseded')),
    evidence              jsonb NOT NULL DEFAULT '{}',
    FOREIGN KEY (deployment_id, revision) REFERENCES deployment_revisions (deployment_id, number)
);
CREATE INDEX approvals_deployment ON approvals (deployment_id, approved_at, id);

CREATE TABLE events (
    seq           bigint PRIMARY KEY,
    at            timestamptz NOT NULL,
    actor         text NOT NULL,
    type          text NOT NULL,
    deployment_id text,
    detail        jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX events_deployment ON events (deployment_id, seq);

-- Observations are not desired state: no event, no transaction rule, replaced in place.
CREATE TABLE observations (
    cluster           text NOT NULL,
    subject_kind      text NOT NULL
                      CHECK (subject_kind IN ('agent', 'agentrelease', 'tool-server',
                                              'model-engine', 'projection')),
    namespace         text NOT NULL,
    name              text NOT NULL,
    observed_at       timestamptz NOT NULL,
    present           boolean NOT NULL DEFAULT true,
    in_flux_inventory boolean,
    document          jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (cluster, subject_kind, namespace, name)
);
