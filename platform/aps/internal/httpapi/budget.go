package httpapi

import (
	"errors"
	"github.com/X-McKay/zetra/platform/aps/internal/kube"
	"github.com/X-McKay/zetra/platform/aps/internal/policy"
	"github.com/X-McKay/zetra/platform/aps/internal/store"
	"net/http"
	"slices"
	"time"
)

type budgetCounts struct {
	Spent    int64 `json:"spent"`
	Inflight int64 `json:"inflight"`
}

// observeBudget exposes no arbitrary ledger keys and no mutation operations.
// Store.Ledger performs its normal expiry charge: observational reads are not a
// guarantee that Redis remains byte-for-byte unchanged.
func (a *API) observeBudget(w http.ResponseWriter, r *http.Request) {
	tok := bearer(r)
	if tok == "" {
		oauthError(w, 401, "invalid_token")
		return
	}
	id, err := a.Kube.Review(r.Context(), tok, policy.PolicyServiceAud)
	if errors.Is(err, kube.ErrUnauthenticated) {
		oauthError(w, 401, "invalid_token")
		return
	}
	if err != nil {
		oauthError(w, 503, "temporarily_unavailable")
		return
	}
	if id == nil || id.Username != a.BudgetObserverSubject {
		forbid(w, "observer_required")
		return
	}
	session, agent := r.PathValue("id"), r.URL.Query().Get("agent")
	if !policy.SessionRe.MatchString(session) || !slices.Contains(a.BudgetObserverAgents, agent) || len(r.URL.Query()) != 1 || len(r.URL.Query()["agent"]) != 1 {
		forbid(w, "fixture_required")
		return
	}
	tenant, owner, err := a.Store.SessionOwner(r.Context(), session)
	if err != nil {
		oauthError(w, 503, "temporarily_unavailable")
		return
	}
	if tenant != "tenant-test" || owner != agent {
		forbid(w, "fixture_required")
		return
	}
	now := a.Now().UTC()
	ss, si, se := a.Store.Ledger(r.Context(), store.SessionLedger(session))
	ds, di, de := a.Store.Ledger(r.Context(), store.AgentLedger(tenant+"/"+agent, now))
	generation, ok, ge := a.Store.ActiveGeneration(r.Context(), session)
	if errors.Join(se, de, ge) != nil {
		oauthError(w, 503, "temporarily_unavailable")
		return
	}
	if !ok {
		generation = 0
	}
	writeJSON(w, 200, map[string]any{"session_id": session, "tenant": tenant, "agent": agent, "generation": generation, "observed_at": now.Format(time.RFC3339Nano), "day_utc": now.Format("2006-01-02"), "session": budgetCounts{ss, si}, "day": budgetCounts{ds, di}})
}
