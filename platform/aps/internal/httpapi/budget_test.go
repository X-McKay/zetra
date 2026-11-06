package httpapi

import (
	"context"
	"encoding/json"
	"github.com/X-McKay/zetra/platform/aps/internal/kube"
	"github.com/X-McKay/zetra/platform/aps/internal/policy"
	"github.com/X-McKay/zetra/platform/aps/internal/store"
	"net/http"
	"strings"
	"testing"
	"time"
)

func TestBudgetObservationRequiresExplicitTrustedFixture(t *testing.T) {
	for _, tc := range []struct {
		name, subject, audience, tenant, agent, method string
		enabled                                        bool
		want                                           int
	}{
		{"disabled", "harness", policy.PolicyServiceAud, "tenant-test", "conformance-budget-day", "GET", false, 404},
		{"allowed", "harness", policy.PolicyServiceAud, "tenant-test", "conformance-budget-day", "GET", true, 200},
		{"wrong audience", "harness", "kubernetes", "tenant-test", "conformance-budget-day", "GET", true, 401},
		{"wrong caller", "runner", policy.PolicyServiceAud, "tenant-test", "conformance-budget-day", "GET", true, 403},
		{"wrong tenant", "harness", policy.PolicyServiceAud, "tenant-a", "conformance-budget-day", "GET", true, 403},
		{"wrong agent", "harness", policy.PolicyServiceAud, "tenant-test", "conformance", "GET", true, 403},
		{"no mutations", "harness", policy.PolicyServiceAud, "tenant-test", "conformance-budget-day", "DELETE", true, 405},
	} {
		t.Run(tc.name, func(t *testing.T) {
			e := newEnv(t)
			if tc.enabled {
				e.api.BudgetObserverSubject = "system:serviceaccount:agent-platform:harness"
				e.api.BudgetObserverAgents = []string{"conformance-budget-day"}
			}
			e.kube.ids["observer-secret|"+tc.audience] = &kube.Identity{Username: "system:serviceaccount:agent-platform:" + tc.subject}
			ctx := context.Background()
			_ = e.st.TouchSession(ctx, sess, tc.tenant, tc.agent, "")
			_ = e.st.SetGeneration(ctx, sess, 2)
			_ = e.st.Reserve(ctx, store.Reservation{Decision: "d", Session: sess, AgentID: tc.tenant + "/" + tc.agent, Amount: 300, SessionBudget: 1000, DayBudget: 2000})
			_, _ = e.st.Settle(ctx, "d", 100)
			e.h = e.api.Handler()
			r := e.do(tc.method, "/v1/conformance/budgets/"+sess+"?agent=conformance-budget-day", "observer-secret", "", "")
			if r.Code != tc.want {
				t.Fatalf("status %d, want %d: %s", r.Code, tc.want, r.Body.String())
			}
			if strings.Contains(r.Body.String(), "observer-secret") || strings.Contains(r.Body.String(), "budget:session:") {
				t.Fatal("secret/key leaked")
			}
			if tc.want == 200 {
				var got struct {
					Session    struct{ Spent, Inflight int64 }
					Day        struct{ Spent, Inflight int64 }
					Generation int
					Agent      string
				}
				if json.Unmarshal(r.Body.Bytes(), &got) != nil || got.Session.Spent != 100 || got.Session.Inflight != 0 || got.Day.Spent != 100 || got.Generation != 2 || got.Agent != tc.agent {
					t.Fatalf("observation %s", r.Body.String())
				}
			}
		})
	}
}

func TestBudgetObservationExpiresHoldsButNeverResetsSpent(t *testing.T) {
	e := newEnv(t)
	e.api.BudgetObserverSubject = runnerUser
	e.api.BudgetObserverAgents = []string{"conformance-budget-day"}
	now := time.Now()
	e.st.Now = func() time.Time { return now }
	e.api.Now = e.st.Now
	ctx := context.Background()
	_ = e.st.TouchSession(ctx, sess, "tenant-test", "conformance-budget-day", "")
	_ = e.st.Reserve(ctx, store.Reservation{Decision: "expired", Session: sess, AgentID: "tenant-test/conformance-budget-day", Amount: 300, SessionBudget: 1000, DayBudget: 2000})
	now = now.Add(store.ReservationTTL)
	e.h = e.api.Handler()
	for i := 0; i < 2; i++ {
		r := e.do(http.MethodGet, "/v1/conformance/budgets/"+sess+"?agent=conformance-budget-day", runnerToken, "", "")
		if r.Code != 200 || !strings.Contains(r.Body.String(), `"spent":300`) {
			t.Fatalf("expiry read: %s", r.Body.String())
		}
	}
}
