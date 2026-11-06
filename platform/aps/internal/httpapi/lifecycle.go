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

// observeLifecycle only returns trusted ownership and presence/TTL metadata.
// Ordinary key expiry can be observed; no cleanup operation is invoked here.
func (a *API) observeLifecycle(w http.ResponseWriter, r *http.Request) {
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
	if id == nil || id.Username != a.LifecycleObserverSubject {
		forbid(w, "observer_required")
		return
	}
	session, agent := r.PathValue("id"), r.URL.Query().Get("agent")
	if !policy.SessionRe.MatchString(session) || !slices.Contains(a.LifecycleObserverAgents, agent) || len(r.URL.Query()) != 1 || len(r.URL.Query()["agent"]) != 1 {
		forbid(w, "fixture_required")
		return
	}

	observation, known, err := a.Store.ObserveLifecycle(r.Context(), session)
	if err != nil {
		oauthError(w, 503, "temporarily_unavailable")
		return
	}
	if !known || observation.Tenant != "tenant-test" || observation.Agent != agent {
		forbid(w, "fixture_required")
		return
	}
	now := a.Now().UTC()
	refreshBacked, accessCurrent := false, false
	if observation.VaultPresent {
		entry, present, err := a.Entries.Load(r.Context(), session)
		if err != nil {
			oauthError(w, 503, "temporarily_unavailable")
			return
		}
		// A concurrent legitimate stop can remove the vault after the atomic presence
		// snapshot. False flags make that sample ineligible as an initial baseline;
		// the next observation can prove cleanup. Never reveal the decrypted entry.
		if present && entry.Tenant == observation.Tenant && entry.Agent == observation.Agent && entry.PrincipalSub != "" && !entry.Expired && now.Before(entry.ExpiresAt) {
			refreshBacked = entry.RefreshToken != "" && entry.AccessToken != ""
			accessCurrent = entry.AccessToken != "" && now.Before(entry.AccessExpiresAt)
		}
	}
	writeJSON(w, 200, struct {
		store.LifecycleObservation
		SessionID     string `json:"session_id"`
		ObservedAt    string `json:"observed_at"`
		RefreshBacked bool   `json:"refresh_backed"`
		AccessCurrent bool   `json:"access_current"`
	}{observation, session, now.Format(time.RFC3339Nano), refreshBacked, accessCurrent})
}
