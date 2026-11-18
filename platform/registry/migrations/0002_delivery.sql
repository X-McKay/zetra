-- Milestone 2 (contract/delivery.md). Evaluations and revocations. Same rules as 0001: column
-- names are the field names of agent_registry/models.py, and nothing is deleted.

CREATE TABLE evaluations (
    id                text PRIMARY KEY,
    deployment_id     text NOT NULL,
    revision          integer NOT NULL,
    attempt           integer NOT NULL CHECK (attempt >= 1),
    suite             jsonb NOT NULL DEFAULT '{}',
    suite_digest      text NOT NULL DEFAULT '',
    created_at        timestamptz NOT NULL,
    -- progress: the only columns of this table that change
    state             text NOT NULL DEFAULT 'pending'
                      CHECK (state IN ('pending', 'claimed', 'passed', 'failed', 'error',
                                       'cancelled')),
    claimed_by        text,
    claim_expires_at  timestamptz,
    evaluator_version text NOT NULL DEFAULT '',
    result            jsonb NOT NULL DEFAULT '{}',
    finished_at       timestamptz,
    UNIQUE (deployment_id, revision, attempt),
    FOREIGN KEY (deployment_id, revision) REFERENCES deployment_revisions (deployment_id, number)
);
CREATE INDEX evaluations_open ON evaluations (state, created_at, id);

-- Indexed from the feed. A revocation may name a release the registry has not recorded yet.
CREATE TABLE revocations (
    id         text PRIMARY KEY,
    kind       text NOT NULL CHECK (kind IN ('agent', 'tool-server', 'skill', 'model')),
    item       text NOT NULL,
    release    text NOT NULL,
    reason     text NOT NULL CHECK (reason IN ('security', 'quality')),
    sessions   text NOT NULL CHECK (sessions IN ('cancel', 'finish')),
    summary    text NOT NULL DEFAULT '',
    revoked_by text NOT NULL,
    revoked_at timestamptz NOT NULL,
    indexed_at timestamptz NOT NULL
);
CREATE INDEX revocations_release ON revocations (kind, item, release);
