package httpapi

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/X-McKay/zetra/platform/aps/internal/kube"
	"github.com/X-McKay/zetra/platform/aps/internal/policy"
	"github.com/X-McKay/zetra/platform/aps/internal/store"
	"github.com/X-McKay/zetra/platform/aps/internal/vault"
)

func TestLifecycleObservationAuthorizationAndNoCredentialDisclosure(t *testing.T) {
	for _, tc := range []struct {
		name, caller, audience, tenant, agent, query, method string
		enabled, known                                       bool
		want                                                 int
	}{
		{"allowed", "conformance-harness", policy.PolicyServiceAud, "tenant-test", "conformance-lifecycle", "agent=conformance-lifecycle", "GET", true, true, 200},
		{"disabled", "conformance-harness", policy.PolicyServiceAud, "tenant-test", "conformance-lifecycle", "agent=conformance-lifecycle", "GET", false, true, 404},
		{"wrong caller", "session-runner", policy.PolicyServiceAud, "tenant-test", "conformance-lifecycle", "agent=conformance-lifecycle", "GET", true, true, 403},
		{"wrong audience", "conformance-harness", "kubernetes", "tenant-test", "conformance-lifecycle", "agent=conformance-lifecycle", "GET", true, true, 401},
		{"wrong tenant", "conformance-harness", policy.PolicyServiceAud, "tenant-other", "conformance-lifecycle", "agent=conformance-lifecycle", "GET", true, true, 403},
		{"wrong agent", "conformance-harness", policy.PolicyServiceAud, "tenant-test", "conformance", "agent=conformance-lifecycle", "GET", true, true, 403},
		{"unknown is not cleaned", "conformance-harness", policy.PolicyServiceAud, "tenant-test", "conformance-lifecycle", "agent=conformance-lifecycle", "GET", true, false, 403},
		{"duplicate agent", "conformance-harness", policy.PolicyServiceAud, "tenant-test", "conformance-lifecycle", "agent=conformance-lifecycle&agent=conformance-lifecycle", "GET", true, true, 403},
		{"arbitrary query", "conformance-harness", policy.PolicyServiceAud, "tenant-test", "conformance-lifecycle", "agent=conformance-lifecycle&key=secret", "GET", true, true, 403},
		{"no mutation", "conformance-harness", policy.PolicyServiceAud, "tenant-test", "conformance-lifecycle", "agent=conformance-lifecycle", "DELETE", true, true, 405},
	} {
		t.Run(tc.name, func(t *testing.T) {
			e := newEnv(t)
			if tc.enabled {
				e.api.LifecycleObserverSubject = "system:serviceaccount:agent-platform:conformance-harness"
				e.api.LifecycleObserverAgents = []string{"conformance-lifecycle"}
			}
			e.kube.ids["observer-secret|"+tc.audience] = &kube.Identity{Username: "system:serviceaccount:agent-platform:" + tc.caller}
			ctx := context.Background()
			if tc.known {
				_ = e.st.TouchSession(ctx, sess, tc.tenant, tc.agent, "secret-handle")
			}
			_ = e.st.SetGeneration(ctx, sess, 1)
			_, _ = e.api.Entries.Create(ctx, sess, &vault.Entry{AccessToken: "secret-vault", RefreshToken: "secret-refresh", ExpiresAt: e.api.Now().Add(time.Hour), AccessExpiresAt: e.api.Now().Add(5 * time.Minute), Tenant: tc.tenant, Agent: tc.agent, PrincipalSub: "private-principal"})
			_ = e.st.PutGrant(ctx, "secret-handle", []byte("secret-grant"), time.Hour)
			e.h = e.api.Handler()
			r := e.do(tc.method, "/v1/conformance/sessions/"+sess+"?"+tc.query, "observer-secret", "", "")
			if r.Code != tc.want {
				t.Fatalf("status %d want %d", r.Code, tc.want)
			}
			for _, secret := range []string{"observer-secret", "secret-handle", "secret-vault", "secret-grant", "secret-refresh", "private-principal", "vault:", "grant:"} {
				if strings.Contains(r.Body.String(), secret) {
					t.Fatal("credential material leaked")
				}
			}
			if tc.want == 200 {
				var got struct {
					store.LifecycleObservation
					SessionID  string    `json:"session_id"`
					ObservedAt time.Time `json:"observed_at"`
				}
				if err := json.Unmarshal(r.Body.Bytes(), &got); err != nil || got.SessionID != sess || got.ObservedAt.IsZero() || !got.GrantPresent || !got.VaultPresent || got.Generation != 1 || got.GrantTTLSeconds <= 600 {
					t.Fatalf("invalid metadata %s", r.Body.String())
				}
				_ = e.st.MarkStopped(ctx, sess)
				_ = e.st.DeleteVault(ctx, sess)
				_ = e.st.CompleteStop(ctx, sess)
				_ = e.st.DeleteGrant(ctx, "secret-handle")
				_, _ = e.st.ListSessions(ctx, time.Now().Add(time.Hour))
				r = e.do("GET", "/v1/conformance/sessions/"+sess+"?"+tc.query, "observer-secret", "", "")
				if r.Code != 200 || json.Unmarshal(r.Body.Bytes(), &got) != nil || got.GrantPresent || got.VaultPresent || got.Generation != 0 || !got.Stopped || got.CleanupPending {
					t.Fatalf("final metadata %s", r.Body.String())
				}
			}
		})
	}
}

func TestLifecycleObserverProvesRefreshBackedCurrentVaultWithoutDisclosingIt(t *testing.T) {
	e := newEnv(t)
	e.api.LifecycleObserverSubject = runnerUser
	e.api.LifecycleObserverAgents = []string{"conformance-lifecycle"}
	e.h = e.api.Handler()
	ctx := context.Background()
	_ = e.st.TouchSession(ctx, sess, "tenant-test", "conformance-lifecycle", "handle")
	en := &vault.Entry{AccessToken: "private-access", RefreshToken: "", PrincipalSub: "private-human", Tenant: "tenant-test", Agent: "conformance-lifecycle", ExpiresAt: e.api.Now().Add(time.Hour), AccessExpiresAt: e.api.Now().Add(5 * time.Minute)}
	_, _ = e.api.Entries.Create(ctx, sess, en)
	for _, refresh := range []bool{false, true} {
		if refresh {
			en.RefreshToken = "private-refresh"
			_ = e.api.Entries.Save(ctx, sess, en)
		}
		response := e.do("GET", "/v1/conformance/sessions/"+sess+"?agent=conformance-lifecycle", runnerToken, "", "")
		var got map[string]any
		_ = json.Unmarshal(response.Body.Bytes(), &got)
		if response.Code != 200 || got["refresh_backed"] != refresh || got["access_current"] != true {
			t.Fatalf("safe flags incorrect: %s", response.Body.String())
		}
		if strings.Contains(response.Body.String(), "private-") {
			t.Fatal("vault material disclosed")
		}
	}
	en.AccessExpiresAt = e.api.Now().Add(-time.Second)
	_ = e.api.Entries.Save(ctx, sess, en)
	response := e.do("GET", "/v1/conformance/sessions/"+sess+"?agent=conformance-lifecycle", runnerToken, "", "")
	if !strings.Contains(response.Body.String(), `"access_current":false`) {
		t.Fatal("expired access reported current")
	}
}
