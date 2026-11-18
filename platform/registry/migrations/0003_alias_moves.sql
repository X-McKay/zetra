-- Milestone 3 (contract/dependencies.md sections 5 and 6). Assessments, alias moves and holds.
-- Same rules as 0001: column names are the field names of agent_registry/models.py, and nothing
-- is deleted.

CREATE TABLE assessments (
    id         text PRIMARY KEY,
    cluster    text NOT NULL,
    alias      text NOT NULL,
    pair       jsonb NOT NULL,
    candidate  jsonb NOT NULL,
    dependants jsonb NOT NULL DEFAULT '[]',
    blocked    boolean NOT NULL,
    digest     text NOT NULL,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL
);

CREATE TABLE alias_moves (
    id                 text PRIMARY KEY,
    assessment_id      text NOT NULL REFERENCES assessments (id),
    cluster            text NOT NULL,
    alias              text NOT NULL,
    candidate          jsonb NOT NULL,
    sessions           text NOT NULL CHECK (sessions IN ('finish', 'cancel')),
    evaluation_seconds integer NOT NULL CHECK (evaluation_seconds >= 1),
    started_by         text NOT NULL,
    started_at         timestamptz NOT NULL,
    -- progress: the only columns of this table that change
    state              text NOT NULL DEFAULT 'holding'
                       CHECK (state IN ('holding', 'held', 'moving', 'evaluating', 'completed',
                                        'cancelled')),
    held_at            timestamptz,
    moved_at           timestamptz,
    evaluating_at      timestamptz,
    deadline           timestamptz,
    finished_at        timestamptz,
    reason             text NOT NULL DEFAULT ''
);
-- An alias has at most one open move.
CREATE UNIQUE INDEX alias_moves_one_open ON alias_moves (cluster, alias)
    WHERE state NOT IN ('completed', 'cancelled');
CREATE INDEX alias_moves_started ON alias_moves (started_at, id);

CREATE TABLE holds (
    move_id         text NOT NULL REFERENCES alias_moves (id),
    deployment_id   text NOT NULL REFERENCES deployments (id),
    -- progress: the only columns of this table that change
    state           text NOT NULL DEFAULT 'holding'
                    CHECK (state IN ('holding', 'held', 'suspended', 'resumed', 'released')),
    reason          text NOT NULL DEFAULT '',
    running         integer,
    acknowledged_at timestamptz,
    ended_at        timestamptz,
    PRIMARY KEY (move_id, deployment_id)
);
-- A deployment has at most one hold that is not resumed or released.
CREATE UNIQUE INDEX holds_one_in_force ON holds (deployment_id)
    WHERE state NOT IN ('resumed', 'released');
