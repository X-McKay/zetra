// Package exchange performs the token exchanges of 9.5 through a provider
// adapter, caches the results encrypted, and refreshes vaulted subject tokens.
package exchange

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/X-McKay/zetra/platform/aps/internal/policy"
)

const idpTimeout = 5 * time.Second

// TokenResponse is the subset of an OAuth token response the APS uses.
type TokenResponse struct {
	AccessToken  string `json:"access_token"`
	RefreshToken string `json:"refresh_token,omitempty"`
	ExpiresIn    int64  `json:"expires_in"`
}

// DelegatedRequest is the client-delegation exchange of 7.5: the APS
// authenticates as the agent client with a pod-bound assertion.
type DelegatedRequest struct {
	ClientID        string
	ClientAssertion string
	SubjectToken    string
	ActorToken      string
	Scope           string
}

// Adapter is the provider-specific half of 9.5, selected by IDP_ADAPTER.
type Adapter interface {
	Delegated(ctx context.Context, r DelegatedRequest) (*TokenResponse, error)
	Agent(ctx context.Context, subjectToken, audience string) (*TokenResponse, error)
	Refresh(ctx context.Context, refreshToken string) (*TokenResponse, error)
}

// Error is an IdP refusal or transport failure. It never carries token material.
type Error struct {
	Class  string // "4xx", "5xx", "timeout", "transport", "decode"
	Status int
	OAuth  string // the IdP's "error" field
}

func (e *Error) Error() string {
	return fmt.Sprintf("idp %s (status %d, error %q)", e.Class, e.Status, e.OAuth)
}

// Keycloak implements Adapter for Keycloak 26 (7.5).
type Keycloak struct {
	TokenEndpoint string
	ClientID      string // agent-policy-service
	ClientSecret  string
	CLIClientID   string // agent-cli, public
	HTTP          *http.Client
}

func (k *Keycloak) Delegated(ctx context.Context, r DelegatedRequest) (*TokenResponse, error) {
	return k.post(ctx, url.Values{
		"grant_type":            {policy.GrantTokenExchange},
		"client_id":             {r.ClientID},
		"client_assertion_type": {policy.AssertionTypeJWT},
		"client_assertion":      {r.ClientAssertion},
		"subject_token":         {r.SubjectToken},
		"subject_token_type":    {policy.TokenTypeAccess},
		"actor_token":           {r.ActorToken},
		"actor_token_type":      {policy.TokenTypeAccess},
		"scope":                 {r.Scope},
		"requested_token_type":  {policy.TokenTypeAccess},
	}, false)
}

func (k *Keycloak) Agent(ctx context.Context, subjectToken, audience string) (*TokenResponse, error) {
	return k.post(ctx, url.Values{
		"grant_type":           {policy.GrantTokenExchange},
		"subject_token":        {subjectToken},
		"subject_token_type":   {policy.TokenTypeAccess},
		"audience":             {audience},
		"requested_token_type": {policy.TokenTypeAccess},
	}, true)
}

func (k *Keycloak) Refresh(ctx context.Context, refreshToken string) (*TokenResponse, error) {
	cli := k.CLIClientID
	if cli == "" {
		cli = "agent-cli"
	}
	return k.post(ctx, url.Values{
		"grant_type":    {"refresh_token"},
		"client_id":     {cli},
		"refresh_token": {refreshToken},
	}, false)
}

// post sends form with a 5 s timeout per attempt: no retry on 4xx, one retry on
// 5xx or timeout.
func (k *Keycloak) post(ctx context.Context, form url.Values, basic bool) (*TokenResponse, error) {
	var last error
	for attempt := 0; attempt < 2; attempt++ {
		tr, err := k.once(ctx, form, basic)
		if err == nil {
			return tr, nil
		}
		last = err
		var e *Error
		if !errors.As(err, &e) || (e.Class != "5xx" && e.Class != "timeout") || ctx.Err() != nil {
			break
		}
	}
	return nil, last
}

func (k *Keycloak) once(ctx context.Context, form url.Values, basic bool) (*TokenResponse, error) {
	ctx, cancel := context.WithTimeout(ctx, idpTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, k.TokenEndpoint, strings.NewReader(form.Encode()))
	if err != nil {
		return nil, &Error{Class: "transport"}
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("Accept", "application/json")
	if basic {
		req.SetBasicAuth(url.QueryEscape(k.ClientID), url.QueryEscape(k.ClientSecret))
	}
	resp, err := k.HTTP.Do(req)
	if err != nil {
		if errors.Is(err, context.DeadlineExceeded) || ctx.Err() != nil {
			return nil, &Error{Class: "timeout"}
		}
		var ne interface{ Timeout() bool }
		if errors.As(err, &ne) && ne.Timeout() {
			return nil, &Error{Class: "timeout"}
		}
		return nil, &Error{Class: "transport"}
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	switch {
	case resp.StatusCode >= 500:
		return nil, &Error{Class: "5xx", Status: resp.StatusCode}
	case resp.StatusCode != http.StatusOK:
		var oe struct {
			Error string `json:"error"`
		}
		_ = json.Unmarshal(body, &oe)
		return nil, &Error{Class: "4xx", Status: resp.StatusCode, OAuth: oe.Error}
	}
	var tr TokenResponse
	if err := json.Unmarshal(body, &tr); err != nil || tr.AccessToken == "" {
		return nil, &Error{Class: "decode", Status: resp.StatusCode}
	}
	return &tr, nil
}
