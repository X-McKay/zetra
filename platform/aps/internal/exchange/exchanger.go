package exchange

import (
	"context"
	"encoding/json"
	"errors"
	"time"

	"golang.org/x/sync/singleflight"
	corev1 "k8s.io/api/core/v1"

	"github.com/X-McKay/zetra/platform/aps/internal/jwtverify"
	"github.com/X-McKay/zetra/platform/aps/internal/kube"
	"github.com/X-McKay/zetra/platform/aps/internal/metrics"
	"github.com/X-McKay/zetra/platform/aps/internal/policy"
	"github.com/X-McKay/zetra/platform/aps/internal/store"
	"github.com/X-McKay/zetra/platform/aps/internal/vault"
)

const (
	cacheMargin   = 30 * time.Second  // exchanged tokens are reused until exp - 30 s
	refreshBefore = 120 * time.Second // vaulted tokens are refreshed when fewer remain
	flightBudget  = 2*idpTimeout + 2*time.Second
)

// Verifier validates an upstream token's signature, iss and exact aud.
type Verifier interface {
	Verify(ctx context.Context, raw, audience string, exact bool) (*jwtverify.Claims, error)
}

// Exchanger implements the Exchange dependency of the decision path.
type Exchanger struct {
	Adapter  Adapter
	Store    store.Store
	Vault    *vault.Vault
	Entries  *vault.Entries
	Verifier Verifier
	Kube     kube.API
	Issuer   string
	Metrics  *metrics.Metrics
	Now      func() time.Time

	group singleflight.Group
}

type cached struct {
	Token string    `json:"token"`
	JTI   string    `json:"jti"`
	Exp   time.Time `json:"exp"`
}

func aad(session, key string) string { return "xchg|" + session + "|" + key }

func (e *Exchanger) lookup(ctx context.Context, session, key string) (*cached, bool) {
	blob, ok, err := e.Store.GetExchanged(ctx, session, key)
	if err != nil || !ok {
		return nil, false
	}
	plain, err := e.Vault.Open(aad(session, key), blob)
	if err != nil {
		return nil, false
	}
	var c cached
	if json.Unmarshal(plain, &c) != nil || !e.Now().Before(c.Exp.Add(-cacheMargin)) {
		return nil, false
	}
	return &c, true
}

// Exchange returns an upstream token for (session, backend, mode), reusing a
// cached one when it has more than 30 s left. Concurrent calls for the same
// session and backend share one IdP request.
func (e *Exchanger) Exchange(ctx context.Context, mode, session, backend, resource, actor string, pod *corev1.Pod) (*policy.Exchanged, error) {
	start := e.Now()
	key := backend + "|" + mode
	if c, ok := e.lookup(ctx, session, key); ok {
		e.Metrics.Exchange(mode, true, e.Now().Sub(start))
		return &policy.Exchanged{Token: c.Token, UpstreamJTI: c.JTI, Cached: true, Duration: e.Now().Sub(start)}, nil
	}
	// The flight is detached from the request so a slow IdP answer is still cached.
	ch := e.group.DoChan(session+"|"+key, func() (any, error) {
		fctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), flightBudget)
		defer cancel()
		if c, ok := e.lookup(fctx, session, key); ok {
			return c, nil
		}
		return e.mint(fctx, mode, session, key, resource, actor, pod)
	})
	select {
	case <-ctx.Done():
		e.Metrics.ExchangeFailure(mode, "timeout")
		return nil, ctx.Err()
	case res := <-ch:
		if res.Err != nil {
			e.Metrics.ExchangeFailure(mode, errClass(res.Err))
			return nil, res.Err
		}
		c := res.Val.(*cached)
		d := e.Now().Sub(start)
		e.Metrics.Exchange(mode, false, d)
		return &policy.Exchanged{Token: c.Token, UpstreamJTI: c.JTI, Duration: d}, nil
	}
}

func errClass(err error) string {
	var ie *Error
	switch {
	case errors.As(err, &ie):
		return ie.Class
	case errors.Is(err, policy.ErrDelegationRequired):
		return "delegation_required"
	default:
		return "other"
	}
}

func (e *Exchanger) mint(ctx context.Context, mode, session, key, resource, actor string, pod *corev1.Pod) (*cached, error) {
	var tr *TokenResponse
	var err error
	switch mode {
	case "agent":
		tr, err = e.Adapter.Agent(ctx, actor, resource)
	case "on-behalf-of":
		tr, err = e.delegated(ctx, session, key, actor, pod)
	default:
		return nil, errors.New("unknown delegation mode")
	}
	if err != nil {
		return nil, err
	}
	claims, err := e.Verifier.Verify(ctx, tr.AccessToken, resource, true)
	if err != nil {
		return nil, &Error{Class: "invalid_token"}
	}
	if mode == "on-behalf-of" && claims.ActSub == "" {
		return nil, &Error{Class: "invalid_token"}
	}
	c := &cached{Token: tr.AccessToken, JTI: claims.JTI, Exp: claims.Expiry}
	if ttl := c.Exp.Add(-cacheMargin).Sub(e.Now()); ttl > 0 {
		if b, err := json.Marshal(c); err == nil {
			if blob, err := e.Vault.Seal(aad(session, key), b); err == nil {
				_ = e.Store.PutExchanged(ctx, session, key, blob, ttl)
			}
		}
	}
	return c, nil
}

func (e *Exchanger) delegated(ctx context.Context, session, key, actor string, pod *corev1.Pod) (*TokenResponse, error) {
	en, ok, err := e.Entries.Load(ctx, session)
	if err != nil {
		return nil, err
	}
	if !ok || en.Expired || !e.Now().Before(en.ExpiresAt) {
		return nil, policy.ErrDelegationRequired
	}
	if en.AccessExpiresAt.Sub(e.Now()) < refreshBefore {
		if en.RefreshToken == "" {
			if !e.Now().Before(en.AccessExpiresAt) {
				return nil, policy.ErrDelegationRequired
			}
		} else if err := e.refresh(ctx, session, en); err != nil {
			return nil, err
		}
	}
	agent := pod.GetLabels()[policy.LabelAgent]
	ns := pod.GetNamespace()
	assertion, err := e.Kube.AgentToken(ctx, ns, "agent-"+agent, pod, e.Issuer, 300)
	if err != nil {
		return nil, &Error{Class: "token_request"}
	}
	return e.Adapter.Delegated(ctx, DelegatedRequest{
		ClientID:        "agent-" + ns + "-" + agent,
		ClientAssertion: assertion,
		SubjectToken:    en.AccessToken,
		ActorToken:      actor,
		Scope:           "mcp:" + backendOf(key),
	})
}

func backendOf(key string) string {
	for i := 0; i < len(key); i++ {
		if key[i] == '|' {
			return key[:i]
		}
	}
	return key
}

// refresh renews the vaulted access token; on failure the entry is marked
// expired and the caller sees ErrDelegationRequired.
func (e *Exchanger) refresh(ctx context.Context, session string, en *vault.Entry) error {
	tr, err := e.Adapter.Refresh(ctx, en.RefreshToken)
	if err != nil {
		en.Expired = true
		_ = e.Entries.Save(ctx, session, en)
		return policy.ErrDelegationRequired
	}
	en.AccessToken = tr.AccessToken
	if tr.RefreshToken != "" {
		en.RefreshToken = tr.RefreshToken
	}
	en.AccessExpiresAt = e.Now().Add(time.Duration(tr.ExpiresIn) * time.Second)
	return e.Entries.Save(ctx, session, en)
}
