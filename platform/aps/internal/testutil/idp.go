// Package testutil provides a fake identity provider for tests: an RSA signing
// key, a JWKS endpoint and a token signer.
package testutil

import (
	"crypto/rand"
	"crypto/rsa"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"regexp"
	"sync/atomic"
	"testing"
	"time"

	"github.com/go-jose/go-jose/v4"
	"github.com/go-jose/go-jose/v4/jwt"
)

const Issuer = "https://idp.example.com/realms/agents"

// Redaction patterns of 13.6.
var (
	JWTRe    = regexp.MustCompile(`eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}`)
	BearerRe = regexp.MustCompile(`Bearer [A-Za-z0-9._~+/=-]{20,}`)
)

// AssertRedacted fails the test if text contains a token-like string.
func AssertRedacted(t testing.TB, text string) {
	t.Helper()
	if m := JWTRe.FindString(text); m != "" {
		t.Fatalf("log contains a JWT: %.20s...", m)
	}
	if m := BearerRe.FindString(text); m != "" {
		t.Fatalf("log contains a bearer token: %.20s...", m)
	}
}

type IdP struct {
	Key     *rsa.PrivateKey
	KID     string
	Server  *httptest.Server
	Fetches atomic.Int32
	signer  jose.Signer
}

func NewIdP(t testing.TB) *IdP {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	p := &IdP{Key: key, KID: "k1"}
	p.Rotate(t, "k1", key)
	p.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		p.Fetches.Add(1)
		set := jose.JSONWebKeySet{Keys: []jose.JSONWebKey{{Key: &p.Key.PublicKey, KeyID: p.KID, Algorithm: "RS256", Use: "sig"}}}
		_ = json.NewEncoder(w).Encode(set)
	}))
	t.Cleanup(p.Server.Close)
	return p
}

// Rotate switches the signing key and the published JWKS.
func (p *IdP) Rotate(t testing.TB, kid string, key *rsa.PrivateKey) {
	t.Helper()
	s, err := jose.NewSigner(jose.SigningKey{Algorithm: jose.RS256, Key: key},
		(&jose.SignerOptions{}).WithType("JWT").WithHeader("kid", kid))
	if err != nil {
		t.Fatal(err)
	}
	p.KID, p.Key, p.signer = kid, key, s
}

// Sign signs claims; iss, iat and exp default to the issuer, now and now+300 s.
func (p *IdP) Sign(t testing.TB, claims map[string]any) string {
	t.Helper()
	c := map[string]any{"iss": Issuer, "iat": time.Now().Unix(), "exp": time.Now().Add(300 * time.Second).Unix()}
	for k, v := range claims {
		if v == nil {
			delete(c, k)
			continue
		}
		c[k] = v
	}
	tok, err := jwt.Signed(p.signer).Claims(c).Serialize()
	if err != nil {
		t.Fatal(err)
	}
	return tok
}

// AgentClaims returns the claims of an agent access token (4.2).
func AgentClaims(jti string) map[string]any {
	return map[string]any{
		"sub": "9b2c0000-agent", "azp": "agent-tenant-a-investigator", "aud": []string{"agent-gateway", "agent-policy-service"},
		"jti": jti, "scope": "agent-gateway", "client_type": "agent", "agent_id": "tenant-a/investigator",
		"tenant": "tenant-a", "spiffe_id": "spiffe://cluster.local/ns/tenant-a/sa/agent-investigator",
	}
}
