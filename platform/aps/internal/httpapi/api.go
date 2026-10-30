// Package httpapi serves the APS HTTP API of 9.7 on :9002.
package httpapi

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"regexp"
	"slices"
	"strings"
	"time"

	corev1 "k8s.io/api/core/v1"

	"github.com/X-McKay/zetra/platform/aps/internal/jwtverify"
	"github.com/X-McKay/zetra/platform/aps/internal/kube"
	"github.com/X-McKay/zetra/platform/aps/internal/policy"
	"github.com/X-McKay/zetra/platform/aps/internal/store"
	"github.com/X-McKay/zetra/platform/aps/internal/vault"
)

const (
	maxBody    = 64 << 10
	idpTimeout = 5 * time.Second
)

var agentNameRe = regexp.MustCompile(`^[a-z0-9-]{1,63}$`)

// Form fields a sandbox may send on the token route (4.2).
var tokenFields = []string{"grant_type", "client_id", "client_assertion_type", "client_assertion", "scope"}

type Verifier interface {
	Verify(ctx context.Context, raw, audience string, exact bool) (*jwtverify.Claims, error)
}

type Pods interface {
	Pod(ns, name string) (*corev1.Pod, bool)
}

type API struct {
	Kube          kube.API
	Store         store.Store
	Entries       *vault.Entries
	Verifier      Verifier
	Pods          Pods
	IssuerURL     string
	TokenEndpoint string
	IdP           *http.Client
	// Full usernames, e.g. system:serviceaccount:agent-platform:session-runner.
	RunnerSubjects     []string
	ReconcilerSubjects []string
	Log                *slog.Logger
	Now                func() time.Time
}

func (a *API) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/token", a.token)
	mux.HandleFunc("POST /v1/grants", a.createGrant)
	mux.HandleFunc("GET /v1/grants/{handle}", a.runner(false, a.getGrant))
	mux.HandleFunc("DELETE /v1/grants/{handle}", a.runner(true, a.deleteGrant))
	mux.HandleFunc("PUT /v1/sessions/{id}/generation", a.runner(false, a.setGeneration))
	mux.HandleFunc("POST /v1/sessions/{id}/stop", a.runner(true, a.stopSession))
	mux.HandleFunc("GET /v1/sessions", a.runner(true, a.listSessions))
	return http.MaxBytesHandler(mux, maxBody)
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func oauthError(w http.ResponseWriter, code int, e string) {
	writeJSON(w, code, map[string]string{"error": e})
}

func forbid(w http.ResponseWriter, reason string) {
	writeJSON(w, http.StatusForbidden, map[string]string{"error": "forbidden", "reason": reason})
}

func (a *API) log() *slog.Logger {
	if a.Log == nil {
		return slog.Default()
	}
	return a.Log
}

func bearer(r *http.Request) string {
	h := r.Header.Get("Authorization")
	if len(h) > 7 && strings.EqualFold(h[:7], "bearer ") {
		return strings.TrimSpace(h[7:])
	}
	return ""
}

// ---- POST /v1/token -------------------------------------------------------

func (a *API) token(w http.ResponseWriter, r *http.Request) {
	raw, err := io.ReadAll(r.Body)
	if err != nil {
		oauthError(w, http.StatusBadRequest, "invalid_request")
		return
	}
	form, err := url.ParseQuery(string(raw))
	if err != nil {
		oauthError(w, http.StatusBadRequest, "invalid_request")
		return
	}
	if form.Get("grant_type") != "client_credentials" {
		a.log().Info("token route refused", "reason", "unsupported_grant_type")
		oauthError(w, http.StatusBadRequest, "unsupported_grant_type")
		return
	}
	for k, v := range form {
		if !slices.Contains(tokenFields, k) || len(v) != 1 {
			a.log().Info("token route refused", "reason", "invalid_request", "field", k)
			if k == "client_secret" {
				oauthError(w, http.StatusUnauthorized, "invalid_client")
				return
			}
			oauthError(w, http.StatusBadRequest, "invalid_request")
			return
		}
	}
	if form.Get("client_assertion_type") != policy.AssertionTypeJWT || form.Get("client_assertion") == "" {
		oauthError(w, http.StatusUnauthorized, "invalid_client")
		return
	}
	id, err := a.Kube.Review(r.Context(), form.Get("client_assertion"), a.IssuerURL)
	if errors.Is(err, kube.ErrUnauthenticated) {
		a.log().Info("token route refused", "reason", "assertion_not_authenticated")
		oauthError(w, http.StatusUnauthorized, "invalid_client")
		return
	}
	if err != nil {
		a.log().Warn("token review failed", "error", err.Error())
		oauthError(w, http.StatusServiceUnavailable, "temporarily_unavailable")
		return
	}
	pod, reason := a.sandboxFor(id, form.Get("client_id"))
	if pod == nil {
		a.log().Info("token route refused", "reason", reason, "user", id.Username, "pod", id.PodName())
		forbid(w, reason)
		return
	}
	a.forwardToken(w, r, raw, pod, form.Get("client_id"))
}

// sandboxFor checks that the assertion's ServiceAccount and bound pod are a
// running sandbox of the agent that client_id names.
func (a *API) sandboxFor(id *kube.Identity, clientID string) (*corev1.Pod, string) {
	parts := strings.Split(id.Username, ":")
	if len(parts) != 4 || parts[0] != "system" || parts[1] != "serviceaccount" {
		return nil, "not_a_service_account"
	}
	ns, sa := parts[2], parts[3]
	agent, ok := strings.CutPrefix(sa, "agent-")
	if !ok || !policy.TenantRe.MatchString(ns) || !agentNameRe.MatchString(agent) {
		return nil, "not_an_agent"
	}
	if clientID != "agent-"+ns+"-"+agent {
		return nil, "client_mismatch"
	}
	name, uid := id.PodName(), id.PodUID()
	if name == "" || uid == "" {
		return nil, "assertion_not_pod_bound"
	}
	pod, ok := a.Pods.Pod(ns, name)
	if !ok || string(pod.UID) != uid || pod.Labels[policy.LabelSandbox] != "true" {
		return nil, "pod_not_sandbox"
	}
	if pod.Labels[policy.LabelAgent] != agent {
		return nil, "pod_agent_mismatch"
	}
	if pod.Status.Phase != corev1.PodRunning || pod.DeletionTimestamp != nil {
		return nil, "pod_not_running"
	}
	return pod, ""
}

func (a *API) forwardToken(w http.ResponseWriter, r *http.Request, raw []byte, pod *corev1.Pod, clientID string) {
	ctx, cancel := context.WithTimeout(r.Context(), idpTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, a.TokenEndpoint, strings.NewReader(string(raw)))
	if err != nil {
		oauthError(w, http.StatusInternalServerError, "server_error")
		return
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("Accept", "application/json")
	resp, err := a.IdP.Do(req)
	if err != nil {
		a.log().Warn("idp token request failed", "error_class", "transport")
		oauthError(w, http.StatusBadGateway, "temporarily_unavailable")
		return
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		oauthError(w, http.StatusBadGateway, "temporarily_unavailable")
		return
	}
	if resp.StatusCode != http.StatusOK {
		w.Header().Set("Content-Type", resp.Header.Get("Content-Type"))
		w.Header().Set("Cache-Control", "no-store")
		w.WriteHeader(resp.StatusCode)
		_, _ = w.Write(body)
		return
	}
	var tr struct {
		AccessToken string `json:"access_token"`
		ExpiresIn   int64  `json:"expires_in"`
	}
	if json.Unmarshal(body, &tr) != nil || tr.AccessToken == "" || tr.ExpiresIn <= 0 {
		oauthError(w, http.StatusBadGateway, "server_error")
		return
	}
	claims, err := a.Verifier.Verify(r.Context(), tr.AccessToken, policy.AgentGatewayAud, false)
	if err != nil || claims.JTI == "" || claims.ClientType != "agent" || (claims.Azp != "" && claims.Azp != clientID) {
		a.log().Warn("idp issued an unacceptable agent token", "pod", pod.Name)
		oauthError(w, http.StatusBadGateway, "server_error")
		return
	}
	exp := a.Now().Add(time.Duration(tr.ExpiresIn) * time.Second)
	pinned, err := a.Store.PinJTI(r.Context(), claims.JTI, string(pod.UID), exp)
	if err != nil || pinned != string(pod.UID) {
		a.log().Warn("token pin failed", "jti", claims.JTI, "pod", pod.Name)
		oauthError(w, http.StatusInternalServerError, "server_error")
		return
	}
	a.log().Info("token issued", "jti", claims.JTI, "agent_id", claims.AgentID, "pod", pod.Name, "namespace", pod.Namespace)
	w.Header().Set("Content-Type", resp.Header.Get("Content-Type"))
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(body)
}

// ---- POST /v1/grants ------------------------------------------------------

type grantRecord struct {
	SessionID string    `json:"session_id"`
	Tenant    string    `json:"tenant"`
	Agent     string    `json:"agent"`
	ExpiresAt time.Time `json:"expires_at"`
}

func newHandle() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return "gh_" + hex.EncodeToString(b)
}

func (a *API) createGrant(w http.ResponseWriter, r *http.Request) {
	tok := bearer(r)
	if tok == "" {
		oauthError(w, http.StatusUnauthorized, "invalid_token")
		return
	}
	claims, err := a.Verifier.Verify(r.Context(), tok, policy.PolicyServiceAud, false)
	if err != nil {
		oauthError(w, http.StatusUnauthorized, "invalid_token")
		return
	}
	var body struct {
		SessionID    string  `json:"session_id"`
		Tenant       string  `json:"tenant"`
		Agent        string  `json:"agent"`
		RefreshToken *string `json:"refresh_token"`
		ExpiresAt    string  `json:"expires_at"`
	}
	if json.NewDecoder(r.Body).Decode(&body) != nil || !policy.SessionRe.MatchString(body.SessionID) ||
		!policy.TenantRe.MatchString(body.Tenant) || !agentNameRe.MatchString(body.Agent) {
		oauthError(w, http.StatusBadRequest, "invalid_request")
		return
	}
	now := a.Now()
	exp, err := time.Parse(time.RFC3339, body.ExpiresAt)
	if err != nil || !exp.After(now) {
		oauthError(w, http.StatusBadRequest, "invalid_request")
		return
	}
	if limit := now.Add(vault.MaxGrantLifetime); exp.After(limit) {
		exp = limit
	}
	if claims.ClientType == "agent" { // agent tokens also carry this audience (12.1); only user tokens are subjects
		forbid(w, "subject_not_user")
		return
	}
	want := "delegation:client:agent-" + body.Tenant + "-" + body.Agent
	if !slices.Contains(strings.Fields(claims.Scope), want) {
		forbid(w, "scope")
		return
	}
	if claims.MayActSub == "" {
		forbid(w, "may_act")
		return
	}
	en := &vault.Entry{
		AccessToken: tok, ExpiresAt: exp, AccessExpiresAt: claims.Expiry,
		Tenant: body.Tenant, Agent: body.Agent, PrincipalSub: claims.Sub,
	}
	if body.RefreshToken != nil {
		en.RefreshToken = *body.RefreshToken
	}
	created, err := a.Entries.Create(r.Context(), body.SessionID, en)
	if err != nil {
		oauthError(w, http.StatusInternalServerError, "server_error")
		return
	}
	if !created {
		writeJSON(w, http.StatusConflict, map[string]string{"error": "conflict"})
		return
	}
	handle := newHandle()
	rec, _ := json.Marshal(grantRecord{SessionID: body.SessionID, Tenant: body.Tenant, Agent: body.Agent, ExpiresAt: exp})
	if err := a.Store.PutGrant(r.Context(), handle, rec, max(exp.Sub(now), time.Second)); err != nil {
		_ = a.Store.DeleteVault(r.Context(), body.SessionID)
		oauthError(w, http.StatusInternalServerError, "server_error")
		return
	}
	_ = a.Store.TouchSession(r.Context(), body.SessionID, body.Tenant, body.Agent, handle)
	a.log().Info("grant stored", "session_id", body.SessionID, "tenant", body.Tenant, "agent", body.Agent, "principal", claims.Sub)
	writeJSON(w, http.StatusCreated, map[string]string{"grant_handle": handle})
}

// ---- runner identity ------------------------------------------------------

type caller struct {
	id         *kube.Identity
	reconciler bool
}

// runner authenticates the Session Runner (and, when allowReconciler, the
// cleanup reconciler) by TokenReview with audience agent-policy-service.
func (a *API) runner(allowReconciler bool, h func(http.ResponseWriter, *http.Request, *caller)) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		tok := bearer(r)
		if tok == "" {
			oauthError(w, http.StatusUnauthorized, "invalid_token")
			return
		}
		id, err := a.Kube.Review(r.Context(), tok, policy.PolicyServiceAud)
		if errors.Is(err, kube.ErrUnauthenticated) {
			oauthError(w, http.StatusUnauthorized, "invalid_token")
			return
		}
		if err != nil {
			a.log().Warn("token review failed", "error", err.Error())
			oauthError(w, http.StatusServiceUnavailable, "temporarily_unavailable")
			return
		}
		c := &caller{id: id}
		switch {
		case slices.Contains(a.RunnerSubjects, id.Username):
		case allowReconciler && slices.Contains(a.ReconcilerSubjects, id.Username):
			c.reconciler = true
		default:
			forbid(w, "caller")
			return
		}
		h(w, r, c)
	}
}

// tenantOK checks the caller's RoleBinding to tenant with a SubjectAccessReview.
func (a *API) tenantOK(w http.ResponseWriter, r *http.Request, c *caller, tenant string) bool {
	ok, err := a.Kube.CanGetReleases(r.Context(), c.id, tenant)
	if err != nil {
		a.log().Warn("subject access review failed", "error", err.Error())
		oauthError(w, http.StatusServiceUnavailable, "temporarily_unavailable")
		return false
	}
	if !ok {
		forbid(w, "tenant")
		return false
	}
	return true
}

func (a *API) loadGrant(r *http.Request, handle string) (*grantRecord, bool, error) {
	b, ok, err := a.Store.GetGrant(r.Context(), handle)
	if err != nil || !ok {
		return nil, false, err
	}
	var g grantRecord
	if err := json.Unmarshal(b, &g); err != nil {
		return nil, false, err
	}
	return &g, true, nil
}

func (a *API) getGrant(w http.ResponseWriter, r *http.Request, c *caller) {
	g, ok, err := a.loadGrant(r, r.PathValue("handle"))
	if err != nil {
		oauthError(w, http.StatusInternalServerError, "server_error")
		return
	}
	if !ok {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "not_found"})
		return
	}
	if !a.tenantOK(w, r, c, g.Tenant) {
		return
	}
	if _, held, err := a.Store.GetVault(r.Context(), g.SessionID); err != nil || !held {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "not_found"})
		return
	}
	writeJSON(w, http.StatusOK, g)
}

func (a *API) deleteGrant(w http.ResponseWriter, r *http.Request, c *caller) {
	handle := r.PathValue("handle")
	g, ok, err := a.loadGrant(r, handle)
	if err != nil {
		oauthError(w, http.StatusInternalServerError, "server_error")
		return
	}
	if !ok {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "not_found"})
		return
	}
	if !a.tenantOK(w, r, c, g.Tenant) {
		return
	}
	ctx := r.Context()
	if err := errors.Join(a.Store.DeleteVault(ctx, g.SessionID), a.Store.DeleteExchanged(ctx, g.SessionID),
		a.Store.DeleteGrant(ctx, handle)); err != nil {
		oauthError(w, http.StatusInternalServerError, "server_error")
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

// ---- sessions -------------------------------------------------------------

// sessionTenant returns the tenant recorded for the session, or fallback.
func (a *API) sessionTenant(r *http.Request, session, fallback string) (string, error) {
	t, ok, err := a.Store.SessionTenant(r.Context(), session)
	if err != nil || ok {
		return t, err
	}
	return fallback, nil
}

func (a *API) setGeneration(w http.ResponseWriter, r *http.Request, c *caller) {
	session := r.PathValue("id")
	var body struct {
		Generation int    `json:"generation"`
		Tenant     string `json:"tenant"` // optional; used only when no record names the tenant
		Agent      string `json:"agent"`  // optional; reported by GET /v1/sessions
	}
	if !policy.SessionRe.MatchString(session) || json.NewDecoder(r.Body).Decode(&body) != nil || body.Generation < 1 ||
		(body.Tenant != "" && !policy.TenantRe.MatchString(body.Tenant)) || (body.Agent != "" && !agentNameRe.MatchString(body.Agent)) {
		oauthError(w, http.StatusBadRequest, "invalid_request")
		return
	}
	tenant, err := a.sessionTenant(r, session, body.Tenant)
	if err != nil {
		oauthError(w, http.StatusInternalServerError, "server_error")
		return
	}
	if tenant != "" && !a.tenantOK(w, r, c, tenant) {
		return
	}
	switch err := a.Store.SetGeneration(r.Context(), session, body.Generation); {
	case errors.Is(err, store.ErrConflict):
		writeJSON(w, http.StatusConflict, map[string]string{"error": "conflict"})
		return
	case err != nil:
		oauthError(w, http.StatusInternalServerError, "server_error")
		return
	}
	_ = a.Store.TouchSession(r.Context(), session, tenant, body.Agent, "")
	a.log().Info("generation recorded", "session_id", session, "generation", body.Generation)
	writeJSON(w, http.StatusOK, map[string]any{"session_id": session, "generation": body.Generation})
}

func (a *API) stopSession(w http.ResponseWriter, r *http.Request, c *caller) {
	session := r.PathValue("id")
	if !policy.SessionRe.MatchString(session) {
		oauthError(w, http.StatusBadRequest, "invalid_request")
		return
	}
	tenant, err := a.sessionTenant(r, session, "")
	if err != nil {
		oauthError(w, http.StatusInternalServerError, "server_error")
		return
	}
	if tenant != "" && !a.tenantOK(w, r, c, tenant) {
		return
	}
	ctx := r.Context()
	if err := errors.Join(a.Store.MarkStopped(ctx, session), a.Store.DeleteVault(ctx, session),
		a.Store.DeleteExchanged(ctx, session), a.Store.ReleaseSession(ctx, session)); err != nil {
		oauthError(w, http.StatusInternalServerError, "server_error")
		return
	}
	a.log().Info("session stopped", "session_id", session)
	writeJSON(w, http.StatusOK, map[string]any{"session_id": session, "stopped": true})
}

func (a *API) listSessions(w http.ResponseWriter, r *http.Request, c *caller) {
	var older time.Duration
	if v := r.URL.Query().Get("older_than"); v != "" {
		d, err := time.ParseDuration(v)
		if err != nil || d < 0 {
			oauthError(w, http.StatusBadRequest, "invalid_request")
			return
		}
		older = d
	}
	all, err := a.Store.ListSessions(r.Context(), a.Now().Add(-older))
	if err != nil {
		oauthError(w, http.StatusInternalServerError, "server_error")
		return
	}
	allowed := map[string]bool{}
	out := []store.SessionInfo{}
	for _, s := range all {
		if s.Tenant != "" {
			ok, seen := allowed[s.Tenant]
			if !seen {
				if ok, err = a.Kube.CanGetReleases(r.Context(), c.id, s.Tenant); err != nil {
					oauthError(w, http.StatusServiceUnavailable, "temporarily_unavailable")
					return
				}
				allowed[s.Tenant] = ok
			}
			if !ok {
				continue
			}
		}
		out = append(out, s)
	}
	writeJSON(w, http.StatusOK, map[string]any{"sessions": out})
}
