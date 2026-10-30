package jwtverify

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"testing"
	"time"

	"github.com/X-McKay/zetra/platform/aps/internal/testutil"
)

func TestVerifyAgentToken(t *testing.T) {
	idp := testutil.NewIdP(t)
	v := New(testutil.Issuer, idp.Server.URL, nil)
	tok := idp.Sign(t, testutil.AgentClaims("jti-1"))
	c, err := v.Verify(context.Background(), tok, "agent-gateway", false)
	if err != nil {
		t.Fatal(err)
	}
	if c.JTI != "jti-1" || c.ClientType != "agent" || c.AgentID != "tenant-a/investigator" || c.Tenant != "tenant-a" {
		t.Fatalf("claims: %+v", c)
	}
	if !v.Fetched() {
		t.Fatal("expected JWKS fetched")
	}
}

func TestVerifyRejects(t *testing.T) {
	idp := testutil.NewIdP(t)
	other, _ := rsa.GenerateKey(rand.Reader, 2048)
	v := New(testutil.Issuer, idp.Server.URL, nil)
	base := testutil.AgentClaims("j")
	with := func(k string, val any) map[string]any {
		c := map[string]any{}
		for kk, vv := range base {
			c[kk] = vv
		}
		c[k] = val
		return c
	}
	cases := map[string]string{
		"wrong issuer":     idp.Sign(t, with("iss", testutil.Issuer+"/")),
		"audience missing": idp.Sign(t, with("aud", []string{"agent-policy-service"})),
		"expired":          idp.Sign(t, with("exp", time.Now().Add(-time.Minute).Unix())),
		"not yet valid":    idp.Sign(t, with("nbf", time.Now().Add(time.Minute).Unix())),
		"no exp":           idp.Sign(t, with("exp", nil)),
		"garbage":          "not-a-jwt",
	}
	for name, tok := range cases {
		if _, err := v.Verify(context.Background(), tok, "agent-gateway", false); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	// Signed by a key the JWKS does not publish under this kid.
	idp2 := testutil.NewIdP(t)
	idp2.Rotate(t, "k1", other)
	if _, err := v.Verify(context.Background(), idp2.Sign(t, base), "agent-gateway", false); err == nil {
		t.Error("foreign signature accepted")
	}
}

func TestVerifyExactAudience(t *testing.T) {
	idp := testutil.NewIdP(t)
	v := New(testutil.Issuer, idp.Server.URL, nil)
	res := "https://mcp.example.com/kubernetes/mcp"
	one := idp.Sign(t, map[string]any{"sub": "alice", "aud": []string{res}})
	two := idp.Sign(t, map[string]any{"sub": "alice", "aud": []string{res, "other"}})
	if _, err := v.Verify(context.Background(), one, res, true); err != nil {
		t.Fatal(err)
	}
	if _, err := v.Verify(context.Background(), two, res, true); err == nil {
		t.Fatal("multi-audience token accepted under exact match")
	}
}

func TestUnknownKidRefetchesOnceThenFails(t *testing.T) {
	idp := testutil.NewIdP(t)
	v := New(testutil.Issuer, idp.Server.URL, nil)
	if _, err := v.Verify(context.Background(), idp.Sign(t, testutil.AgentClaims("a")), "agent-gateway", false); err != nil {
		t.Fatal(err)
	}
	if n := idp.Fetches.Load(); n != 1 {
		t.Fatalf("fetches = %d", n)
	}
	// Rotation: the new kid is unknown to the cache, so one refetch finds it.
	k2, _ := rsa.GenerateKey(rand.Reader, 2048)
	idp.Rotate(t, "k2", k2)
	now := time.Now().Add(refetchInterval)
	v.Now = func() time.Time { return now }
	if _, err := v.Verify(context.Background(), idp.Sign(t, testutil.AgentClaims("b")), "agent-gateway", false); err != nil {
		t.Fatal(err)
	}
	if n := idp.Fetches.Load(); n != 2 {
		t.Fatalf("fetches = %d, want 2", n)
	}
	// A token with a kid nobody publishes fails without another fetch inside the interval.
	idp3 := testutil.NewIdP(t)
	idp3.Rotate(t, "k9", k2)
	if _, err := v.Verify(context.Background(), idp3.Sign(t, testutil.AgentClaims("c")), "agent-gateway", false); err == nil {
		t.Fatal("unknown kid accepted")
	}
	if n := idp.Fetches.Load(); n != 2 {
		t.Fatalf("fetches = %d, want 2", n)
	}
}
