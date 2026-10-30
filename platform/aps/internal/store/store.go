// Package store is the APS shared state of 9.1 and 9.4: token pins, active
// generations, stopped sessions, vault and grant records, exchanged tokens and
// the budget ledger. Redis serves any number of replicas; Memory is for a single
// replica in development and for tests.
package store

import (
	"context"
	"errors"
	"time"
)

var (
	// ErrConflict is returned when a generation is lower than the active one.
	ErrConflict = errors.New("generation is not increasing")
	// ErrNotFound is returned by Settle when no reservation exists for the decision.
	ErrNotFound = errors.New("not found")
)

const (
	SessionStateTTL  = 24 * time.Hour // active generations and stopped sessions (9.4)
	ReservationTTL   = 130 * time.Second
	sessionLedgerTTL = 24 * time.Hour
	dayLedgerTTL     = 48 * time.Hour
)

// Reservation is one admitted inference request's hold on both budgets.
type Reservation struct {
	Decision      string
	Session       string
	AgentID       string
	Amount        int64
	SessionBudget int64
	DayBudget     int64
}

// SessionInfo is one row of GET /v1/sessions.
type SessionInfo struct {
	SessionID   string  `json:"session_id"`
	Tenant      string  `json:"tenant"`
	Agent       string  `json:"agent"`
	Generation  *int    `json:"generation"`
	GrantHandle *string `json:"grant_handle"` // null when the session has no grant

	Since        time.Time `json:"-"`
	HasGrant     bool      `json:"-"`
	Reservations int64     `json:"-"`
	Stopped      bool      `json:"-"`
}

type Store interface {
	Ping(ctx context.Context) error

	// PinJTI pins jti to podUID unless it is already pinned, and returns the pinned UID.
	PinJTI(ctx context.Context, jti, podUID string, exp time.Time) (string, error)

	// SetGeneration records gen; an equal value is accepted, a lower one is ErrConflict.
	SetGeneration(ctx context.Context, session string, gen int) error
	ActiveGeneration(ctx context.Context, session string) (int, bool, error)
	MarkStopped(ctx context.Context, session string) error
	IsStopped(ctx context.Context, session string) (bool, error)

	// PutVault stores blob only if no entry exists; false means one already exists.
	PutVault(ctx context.Context, session string, blob []byte, ttl time.Duration) (bool, error)
	SetVault(ctx context.Context, session string, blob []byte, ttl time.Duration) error
	GetVault(ctx context.Context, session string) ([]byte, bool, error)
	DeleteVault(ctx context.Context, session string) error
	CountVault(ctx context.Context) (int64, error)

	PutGrant(ctx context.Context, handle string, rec []byte, ttl time.Duration) error
	GetGrant(ctx context.Context, handle string) ([]byte, bool, error)
	DeleteGrant(ctx context.Context, handle string) error

	PutExchanged(ctx context.Context, session, key string, blob []byte, ttl time.Duration) error
	GetExchanged(ctx context.Context, session, key string) ([]byte, bool, error)
	DeleteExchanged(ctx context.Context, session string) error

	// Reserve admits r only if spent+inflight+amount <= budget on both ledgers,
	// atomically; otherwise it returns policy.ErrBudget.
	Reserve(ctx context.Context, r Reservation) error
	// Settle replaces the reservation for decision with actual tokens. It returns
	// false when the reservation had already expired into spent or never existed.
	Settle(ctx context.Context, decision string, actual int64) (bool, error)
	// ReleaseSession drops the session ledger's open reservations (session stop).
	ReleaseSession(ctx context.Context, session string) error
	// Ledger returns spent and inflight for a ledger key after charging expired reservations.
	Ledger(ctx context.Context, key string) (spent, inflight int64, err error)

	// TouchSession records the session's tenant and agent (first writer wins) and optional grant handle.
	TouchSession(ctx context.Context, session, tenant, agent, grantHandle string) error
	SessionTenant(ctx context.Context, session string) (string, bool, error)
	ListSessions(ctx context.Context, olderThan time.Time) ([]SessionInfo, error)
}

// SessionLedger and AgentLedger are the ledger keys of 9.3 step 10.
func SessionLedger(session string) string { return "budget:session:" + session }

func AgentLedger(agentID string, now time.Time) string {
	return "budget:agent:" + agentID + ":" + now.UTC().Format("2006-01-02")
}
