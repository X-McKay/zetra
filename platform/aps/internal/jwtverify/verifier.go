// Package jwtverify validates IdP-issued JWTs against the IdP JWKS (9.2, 9.4).
package jwtverify

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"slices"
	"sync"
	"time"

	"github.com/go-jose/go-jose/v4"
	"github.com/go-jose/go-jose/v4/jwt"
)

const (
	jwksTTL         = 10 * time.Minute
	refetchInterval = 10 * time.Second // bounds unknown-kid refetches
	leeway          = 5 * time.Second
)

var algs = []jose.SignatureAlgorithm{jose.RS256, jose.RS384, jose.RS512, jose.PS256, jose.PS384, jose.PS512, jose.ES256, jose.ES384, jose.ES512, jose.EdDSA}

// Claims are the verified claims of any IdP token the APS validates.
type Claims struct {
	Sub, JTI, Issuer, Azp, Scope          string
	ClientType, AgentID, Tenant, SpiffeID string
	MayActSub, ActSub                     string
	Audience                              []string
	Expiry, NotBefore                     time.Time
}

type custom struct {
	Azp        string `json:"azp"`
	Scope      string `json:"scope"`
	ClientType string `json:"client_type"`
	AgentID    string `json:"agent_id"`
	Tenant     string `json:"tenant"`
	SpiffeID   string `json:"spiffe_id"`
	MayAct     struct {
		Sub string `json:"sub"`
	} `json:"may_act"`
	Act struct {
		Sub string `json:"sub"`
	} `json:"act"`
}

// Verifier checks signature, iss (exact), aud, exp and nbf.
type Verifier struct {
	Issuer  string
	JWKSURL string
	Client  *http.Client
	Now     func() time.Time

	mu        sync.Mutex
	keys      map[string]jose.JSONWebKey
	fetchedAt time.Time
	lastTry   time.Time
}

func New(issuer, jwksURL string, client *http.Client) *Verifier {
	if client == nil {
		client = &http.Client{Timeout: 5 * time.Second}
	}
	return &Verifier{Issuer: issuer, JWKSURL: jwksURL, Client: client, Now: time.Now}
}

// Fetched reports whether the JWKS was fetched at least once (readiness).
func (v *Verifier) Fetched() bool {
	v.mu.Lock()
	defer v.mu.Unlock()
	return !v.fetchedAt.IsZero()
}

// Refresh fetches the JWKS now.
func (v *Verifier) Refresh(ctx context.Context) error {
	v.mu.Lock()
	defer v.mu.Unlock()
	return v.fetchLocked(ctx)
}

func (v *Verifier) fetchLocked(ctx context.Context) error {
	v.lastTry = v.Now()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, v.JWKSURL, nil)
	if err != nil {
		return err
	}
	resp, err := v.Client.Do(req)
	if err != nil {
		return fmt.Errorf("jwks fetch: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("jwks fetch: status %d", resp.StatusCode)
	}
	var set jose.JSONWebKeySet
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&set); err != nil {
		return fmt.Errorf("jwks decode: %w", err)
	}
	keys := make(map[string]jose.JSONWebKey, len(set.Keys))
	for _, k := range set.Keys {
		if k.Use == "" || k.Use == "sig" {
			keys[k.KeyID] = k
		}
	}
	v.keys, v.fetchedAt = keys, v.Now()
	return nil
}

func (v *Verifier) key(ctx context.Context, kid string) (jose.JSONWebKey, error) {
	v.mu.Lock()
	defer v.mu.Unlock()
	now := v.Now()
	if v.fetchedAt.IsZero() || now.Sub(v.fetchedAt) > jwksTTL {
		if err := v.fetchLocked(ctx); err != nil && v.keys == nil {
			return jose.JSONWebKey{}, err
		}
	}
	if k, ok := v.keys[kid]; ok {
		return k, nil
	}
	// Unknown kid: refetch once, then fail.
	if now.Sub(v.lastTry) >= refetchInterval {
		if err := v.fetchLocked(ctx); err != nil {
			return jose.JSONWebKey{}, err
		}
		if k, ok := v.keys[kid]; ok {
			return k, nil
		}
	}
	return jose.JSONWebKey{}, errors.New("unknown kid")
}

// Verify validates raw. When exact is true, aud must be exactly [audience];
// otherwise aud must contain audience.
func (v *Verifier) Verify(ctx context.Context, raw, audience string, exact bool) (*Claims, error) {
	tok, err := jwt.ParseSigned(raw, algs)
	if err != nil {
		return nil, err
	}
	if len(tok.Headers) != 1 {
		return nil, errors.New("expected one signature")
	}
	k, err := v.key(ctx, tok.Headers[0].KeyID)
	if err != nil {
		return nil, err
	}
	var std jwt.Claims
	var c custom
	if err := tok.Claims(k.Key, &std, &c); err != nil {
		return nil, err
	}
	if std.Expiry == nil {
		return nil, errors.New("missing exp")
	}
	if err := std.ValidateWithLeeway(jwt.Expected{Issuer: v.Issuer, AnyAudience: jwt.Audience{audience}, Time: v.Now()}, leeway); err != nil {
		return nil, err
	}
	if exact && (len(std.Audience) != 1 || std.Audience[0] != audience) {
		return nil, errors.New("audience is not exactly the requested resource")
	}
	if !slices.Contains([]string(std.Audience), audience) {
		return nil, errors.New("audience mismatch")
	}
	out := &Claims{
		Sub: std.Subject, JTI: std.ID, Issuer: std.Issuer, Azp: c.Azp, Scope: c.Scope,
		ClientType: c.ClientType, AgentID: c.AgentID, Tenant: c.Tenant, SpiffeID: c.SpiffeID,
		MayActSub: c.MayAct.Sub, ActSub: c.Act.Sub, Audience: std.Audience, Expiry: std.Expiry.Time(),
	}
	if std.NotBefore != nil {
		out.NotBefore = std.NotBefore.Time()
	}
	return out, nil
}
