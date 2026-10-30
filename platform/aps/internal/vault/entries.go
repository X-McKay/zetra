package vault

import (
	"context"
	"encoding/json"
	"time"

	"github.com/X-McKay/zetra/platform/aps/internal/store"
)

// MaxGrantLifetime caps a vault entry at the session maximum (3.4).
const MaxGrantLifetime = 8 * time.Hour

// Entry is one vaulted subject token (9.7).
type Entry struct {
	AccessToken     string    `json:"access_token"`
	RefreshToken    string    `json:"refresh_token,omitempty"`
	ExpiresAt       time.Time `json:"expires_at"`        // grant end, at most 8 h
	AccessExpiresAt time.Time `json:"access_expires_at"` // the access token's exp
	Tenant          string    `json:"tenant"`
	Agent           string    `json:"agent"`
	PrincipalSub    string    `json:"principal_sub"`
	Expired         bool      `json:"expired,omitempty"`
}

// Entries stores Entry values sealed with the session ID as additional data.
type Entries struct {
	Store store.Store
	Vault *Vault
	Now   func() time.Time
}

func (e *Entries) ttl(en *Entry) time.Duration {
	return max(en.ExpiresAt.Sub(e.Now()), time.Second)
}

func (e *Entries) seal(session string, en *Entry) ([]byte, error) {
	b, err := json.Marshal(en)
	if err != nil {
		return nil, err
	}
	return e.Vault.Seal(session, b)
}

// Create stores en only if the session has no entry; false means it already has one.
func (e *Entries) Create(ctx context.Context, session string, en *Entry) (bool, error) {
	blob, err := e.seal(session, en)
	if err != nil {
		return false, err
	}
	return e.Store.PutVault(ctx, session, blob, e.ttl(en))
}

func (e *Entries) Save(ctx context.Context, session string, en *Entry) error {
	blob, err := e.seal(session, en)
	if err != nil {
		return err
	}
	return e.Store.SetVault(ctx, session, blob, e.ttl(en))
}

func (e *Entries) Load(ctx context.Context, session string) (*Entry, bool, error) {
	blob, ok, err := e.Store.GetVault(ctx, session)
	if err != nil || !ok {
		return nil, false, err
	}
	plain, err := e.Vault.Open(session, blob)
	if err != nil {
		return nil, false, err
	}
	var en Entry
	if err := json.Unmarshal(plain, &en); err != nil {
		return nil, false, err
	}
	return &en, true, nil
}

// Usable reports whether the session holds an unexpired, unrevoked grant.
func (e *Entries) Usable(ctx context.Context, session string) (bool, error) {
	en, ok, err := e.Load(ctx, session)
	if err != nil || !ok {
		return false, err
	}
	return !en.Expired && e.Now().Before(en.ExpiresAt), nil
}

func (e *Entries) Principal(ctx context.Context, session string) (string, error) {
	en, ok, err := e.Load(ctx, session)
	if err != nil || !ok {
		return "", err
	}
	return en.PrincipalSub, nil
}
