package httpapi

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/X-McKay/zetra/platform/aps/internal/jwtverify"
	"github.com/X-McKay/zetra/platform/aps/internal/kube"
	"github.com/X-McKay/zetra/platform/aps/internal/policy"
	"github.com/X-McKay/zetra/platform/aps/internal/store"
	"github.com/X-McKay/zetra/platform/aps/internal/testutil"
	"github.com/X-McKay/zetra/platform/aps/internal/vault"
)

const (
	sess         = "6f1d2c3e-0000-4000-8000-000000000001"
	runnerToken  = "runner-projected-token"
	reconToken   = "reconciler-projected-token"
	strangerTok  = "some-other-sa-token"
	runnerUser   = "system:serviceaccount:agent-platform:session-runner"
	reconUser    = "system:serviceaccount:agent-platform:cleanup-reconciler"
	assertionOK  = "projected-token-sandbox"
	assertionAPI = "api-audience-token"
)

type fakeKube struct {
	ids     map[string]*kube.Identity // token|audience -> identity
	tenants map[string]bool
}

func (f *fakeKube) Review(_ context.Context, token, aud string) (*kube.Identity, error) {
	if id, ok := f.ids[token+"|"+aud]; ok {
		return id, nil
	}
	return nil, kube.ErrUnauthenticated
}

func (f *fakeKube) CanGetReleases(_ context.Context, _ *kube.Identity, ns string) (bool, error) {
	return f.tenants[ns], nil
}

func (f *fakeKube) AgentToken(context.Context, string, string, *corev1.Pod, string, int64) (string, error) {
	return "", nil
}

type pods map[string]*corev1.Pod

func (p pods) Pod(ns, name string) (*corev1.Pod, bool) { v, ok := p[ns+"/"+name]; return v, ok }

type env struct {
	api     *API
	h       http.Handler
	idp     *testutil.IdP
	st      *store.Memory
	kube    *fakeKube
	pods    pods
	logs    *bytes.Buffer
	idpHits atomic.Int32
	idpForm string
}

type interruptedStopStore struct {
	store.Store
	failExchangeDelete bool
	failCompletion     bool
}

func (s *interruptedStopStore) DeleteExchanged(ctx context.Context, session string) error {
	if s.failExchangeDelete {
		return errors.New("exchange cleanup unavailable")
	}
	return s.Store.DeleteExchanged(ctx, session)
}

func (s *interruptedStopStore) CompleteStop(ctx context.Context, session string) error {
	if s.failCompletion {
		return errors.New("completion receipt unavailable")
	}
	return s.Store.CompleteStop(ctx, session)
}

func TestStopCompletionFailureRemainsDiscoverable(t *testing.T) {
	e := newEnv(t)
	ctx := context.Background()
	_ = e.st.TouchSession(ctx, sess, "tenant-a", "investigator", "")
	_ = e.st.SetGeneration(ctx, sess, 1)
	interrupted := &interruptedStopStore{Store: e.st, failCompletion: true}
	e.api.Store = interrupted
	if w := e.do("POST", "/v1/sessions/"+sess+"/stop", reconToken, "application/json", "{}"); w.Code != 500 {
		t.Fatalf("completion failure: %d", w.Code)
	}
	list, err := e.st.ListSessions(ctx, time.Now())
	if err != nil || len(list) != 1 || !list[0].CleanupPending {
		t.Fatalf("pending completion receipt lost: %+v %v", list, err)
	}
	interrupted.failCompletion = false
	if w := e.do("POST", "/v1/sessions/"+sess+"/stop", reconToken, "application/json", "{}"); w.Code != 200 {
		t.Fatalf("retry completion: %d", w.Code)
	}
	list, err = e.st.ListSessions(ctx, time.Now())
	if err != nil || len(list) != 0 {
		t.Fatalf("completed cleanup retained: %+v %v", list, err)
	}
}

func TestPartialStopRemainsDiscoverableUntilCleanupCompletes(t *testing.T) {
	e := newEnv(t)
	ctx := context.Background()
	_ = e.st.TouchSession(ctx, sess, "tenant-a", "investigator", "")
	_ = e.st.SetGeneration(ctx, sess, 1)
	_ = e.st.PutExchanged(ctx, sess, "backend", []byte("cached"), time.Hour)
	interrupted := &interruptedStopStore{Store: e.st, failExchangeDelete: true}
	e.api.Store = interrupted
	if w := e.do("POST", "/v1/sessions/"+sess+"/stop", reconToken, "application/json", "{}"); w.Code != 500 {
		t.Fatalf("partial stop: %d", w.Code)
	}
	if _, active, _ := e.st.ActiveGeneration(ctx, sess); active {
		t.Fatal("active generation retained after partial stop")
	}
	var list struct {
		Sessions []map[string]any `json:"sessions"`
	}
	w := e.do("GET", "/v1/sessions", reconToken, "", "")
	_ = json.Unmarshal(w.Body.Bytes(), &list)
	if w.Code != 200 || len(list.Sessions) != 1 || list.Sessions[0]["session_id"] != sess {
		t.Fatalf("partial cleanup lost from discovery: %d %s", w.Code, w.Body)
	}
	interrupted.failExchangeDelete = false
	if w := e.do("POST", "/v1/sessions/"+sess+"/stop", reconToken, "application/json", "{}"); w.Code != 200 {
		t.Fatalf("retry stop: %d", w.Code)
	}
	if _, held, _ := e.st.GetExchanged(ctx, sess, "backend"); held {
		t.Fatal("exchanged token survived retry")
	}
	if w := e.do("GET", "/v1/sessions", reconToken, "", ""); !strings.Contains(w.Body.String(), `"sessions":[]`) {
		t.Fatalf("completed cleanup retained: %s", w.Body)
	}
}

func newEnv(t *testing.T) *env {
	e := &env{idp: testutil.NewIdP(t), st: store.NewMemory(), logs: &bytes.Buffer{}}
	sandbox := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "s-1-g1", Namespace: "tenant-a", UID: "pod-uid-1",
			Labels: map[string]string{policy.LabelSandbox: "true", policy.LabelAgent: "investigator"}},
		Status: corev1.PodStatus{Phase: corev1.PodRunning},
	}
	plain := sandbox.DeepCopy()
	plain.Name, plain.UID, plain.Labels = "debug", "pod-uid-2", map[string]string{policy.LabelAgent: "investigator"}
	e.pods = pods{"tenant-a/s-1-g1": sandbox, "tenant-a/debug": plain}
	saUser := "system:serviceaccount:tenant-a:agent-investigator"
	e.kube = &fakeKube{
		ids: map[string]*kube.Identity{
			assertionOK + "|" + testutil.Issuer: {Username: saUser, Extra: map[string][]string{
				kube.ExtraPodName: {"s-1-g1"}, kube.ExtraPodUID: {"pod-uid-1"}}},
			"projected-token-debug|" + testutil.Issuer: {Username: saUser, Extra: map[string][]string{
				kube.ExtraPodName: {"debug"}, kube.ExtraPodUID: {"pod-uid-2"}}},
			runnerToken + "|" + policy.PolicyServiceAud: {Username: runnerUser},
			reconToken + "|" + policy.PolicyServiceAud:  {Username: reconUser},
			strangerTok + "|" + policy.PolicyServiceAud: {Username: "system:serviceaccount:agent-platform:default"},
		},
		tenants: map[string]bool{"tenant-a": true},
	}
	tokenEP := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		e.idpHits.Add(1)
		b, _ := io.ReadAll(r.Body)
		e.idpForm = string(b)
		w.Header().Set("Content-Type", "application/json")
		tok := e.idp.Sign(t, testutil.AgentClaims("jti-issued"))
		_, _ = w.Write([]byte(`{"access_token":"` + tok + `","expires_in":300,"token_type":"Bearer","scope":"agent-gateway"}`))
	}))
	t.Cleanup(tokenEP.Close)
	key := make([]byte, 32)
	_, _ = rand.Read(key)
	v, _ := vault.New(key)
	e.api = &API{
		Kube: e.kube, Store: e.st, Entries: &vault.Entries{Store: e.st, Vault: v, Now: time.Now},
		Verifier: jwtverify.New(testutil.Issuer, e.idp.Server.URL, nil), Pods: e.pods,
		IssuerURL: testutil.Issuer, TokenEndpoint: tokenEP.URL, IdP: tokenEP.Client(),
		RunnerSubjects: []string{runnerUser}, ReconcilerSubjects: []string{reconUser},
		Log: slog.New(slog.NewJSONHandler(e.logs, nil)), Now: time.Now,
	}
	e.h = e.api.Handler()
	t.Cleanup(func() { testutil.AssertRedacted(t, e.logs.String()) })
	return e
}

func (e *env) do(method, path, auth, ctype, body string) *httptest.ResponseRecorder {
	r := httptest.NewRequest(method, path, strings.NewReader(body))
	if auth != "" {
		r.Header.Set("Authorization", "Bearer "+auth)
	}
	if ctype != "" {
		r.Header.Set("Content-Type", ctype)
	}
	w := httptest.NewRecorder()
	e.h.ServeHTTP(w, r)
	return w
}

func tokenForm(mut func(url.Values)) string {
	v := url.Values{
		"grant_type": {"client_credentials"}, "client_id": {"agent-tenant-a-investigator"},
		"client_assertion_type": {policy.AssertionTypeJWT}, "client_assertion": {assertionOK}, "scope": {"agent-gateway"},
	}
	if mut != nil {
		mut(v)
	}
	return v.Encode()
}

const form = "application/x-www-form-urlencoded"

func TestTokenRouteForwardsAndPins(t *testing.T) {
	e := newEnv(t)
	body := tokenForm(nil)
	w := e.do("POST", "/v1/token", "", form, body)
	if w.Code != 200 || e.idpHits.Load() != 1 {
		t.Fatalf("status %d hits %d: %s", w.Code, e.idpHits.Load(), w.Body)
	}
	if e.idpForm != body {
		t.Fatal("form not forwarded identically")
	}
	var out map[string]any
	_ = json.Unmarshal(w.Body.Bytes(), &out)
	if out["expires_in"].(float64) != 300 || out["token_type"] != "Bearer" {
		t.Fatalf("IdP JSON altered: %v", out)
	}
	if p, _ := e.st.PinJTI(context.Background(), "jti-issued", "other", time.Now().Add(time.Minute)); p != "pod-uid-1" {
		t.Fatalf("jti pinned to %q", p)
	}
}

// T5 logic: other grants, other clients, unbound or non-sandbox assertions are
// refused before the IdP sees them.
func TestT5TokenRouteRefusals(t *testing.T) {
	cases := []struct {
		name string
		body string
		code int
	}{
		{"password grant", url.Values{"grant_type": {"password"}, "client_id": {"agent-cli"}, "username": {"alice"}, "password": {"x"}}.Encode(), 400},
		{"token exchange grant", tokenForm(func(v url.Values) { v.Set("grant_type", policy.GrantTokenExchange) }), 400},
		{"client secret", tokenForm(func(v url.Values) { v.Del("client_assertion"); v.Set("client_secret", "s") }), 401},
		{"API-audience token", tokenForm(func(v url.Values) { v.Set("client_assertion", assertionAPI) }), 401},
		{"human token", tokenForm(func(v url.Values) { v.Set("client_assertion", "eyJhuman") }), 401},
		{"other client", tokenForm(func(v url.Values) { v.Set("client_id", "agent-tenant-a-other") }), 403},
		{"agent-cli client", tokenForm(func(v url.Values) { v.Set("client_id", "agent-cli") }), 403},
		{"non-sandbox pod", tokenForm(func(v url.Values) { v.Set("client_assertion", "projected-token-debug") }), 403},
		{"extra field", tokenForm(func(v url.Values) { v.Set("audience", "x") }), 400},
		{"duplicate field", tokenForm(func(v url.Values) { v.Add("client_id", "agent-tenant-a-investigator") }), 400},
	}
	for _, c := range cases {
		e := newEnv(t)
		w := e.do("POST", "/v1/token", "", form, c.body)
		if w.Code != c.code {
			t.Errorf("%s: status %d, want %d (%s)", c.name, w.Code, c.code, w.Body)
		}
		if e.idpHits.Load() != 0 {
			t.Errorf("%s: forwarded to the IdP", c.name)
		}
	}
	// Terminating pod and agent-label mismatch.
	e := newEnv(t)
	ts := metav1.Now()
	e.pods["tenant-a/s-1-g1"].DeletionTimestamp = &ts
	if w := e.do("POST", "/v1/token", "", form, tokenForm(nil)); w.Code != 403 || e.idpHits.Load() != 0 {
		t.Errorf("terminating pod: %d", w.Code)
	}
	e = newEnv(t)
	e.pods["tenant-a/s-1-g1"].Labels[policy.LabelAgent] = "other"
	if w := e.do("POST", "/v1/token", "", form, tokenForm(nil)); w.Code != 403 || e.idpHits.Load() != 0 {
		t.Errorf("agent label mismatch: %d", w.Code)
	}
}

func grantBody(tenant, agent string) string {
	b, _ := json.Marshal(map[string]any{"session_id": sess, "tenant": tenant, "agent": agent,
		"refresh_token": "rt-secret-value", "expires_at": time.Now().Add(2 * time.Hour).UTC().Format(time.RFC3339)})
	return string(b)
}

func (e *env) subject(t *testing.T, scope string, mayAct bool) string {
	c := map[string]any{"sub": "alice", "aud": []string{"agent-policy-service"}, "scope": scope}
	if mayAct {
		c["may_act"] = map[string]any{"sub": "9b2c"}
	}
	return e.idp.Sign(t, c)
}

const consent = "openid offline_access agent-policy-service delegation:client:agent-tenant-a-investigator"

func TestGrantsLifecycle(t *testing.T) {
	e := newEnv(t)
	sub := e.subject(t, consent, true)
	w := e.do("POST", "/v1/grants", sub, "application/json", grantBody("tenant-a", "investigator"))
	if w.Code != 201 {
		t.Fatalf("create %d %s", w.Code, w.Body)
	}
	var created map[string]string
	_ = json.Unmarshal(w.Body.Bytes(), &created)
	handle := created["grant_handle"]
	if !strings.HasPrefix(handle, "gh_") {
		t.Fatal(handle)
	}
	if w := e.do("POST", "/v1/grants", sub, "application/json", grantBody("tenant-a", "investigator")); w.Code != 409 {
		t.Fatalf("second deposit %d", w.Code)
	}
	w = e.do("GET", "/v1/grants/"+handle, runnerToken, "", "")
	if w.Code != 200 {
		t.Fatalf("get %d", w.Code)
	}
	if strings.Contains(w.Body.String(), sub) || strings.Contains(w.Body.String(), "rt-secret-value") {
		t.Fatal("grant lookup returned token material")
	}
	var g map[string]any
	_ = json.Unmarshal(w.Body.Bytes(), &g)
	if g["session_id"] != sess || g["tenant"] != "tenant-a" || g["agent"] != "investigator" || g["expires_at"] == nil {
		t.Fatalf("grant %v", g)
	}
	// The reconciler cannot read grants; other tenants are refused.
	if w := e.do("GET", "/v1/grants/"+handle, reconToken, "", ""); w.Code != 403 {
		t.Fatalf("reconciler get %d", w.Code)
	}
	e.kube.tenants["tenant-a"] = false
	if w := e.do("GET", "/v1/grants/"+handle, runnerToken, "", ""); w.Code != 403 {
		t.Fatalf("other-tenant get %d", w.Code)
	}
	e.kube.tenants["tenant-a"] = true
	if w := e.do("DELETE", "/v1/grants/"+handle, reconToken, "", ""); w.Code != 204 {
		t.Fatalf("delete %d", w.Code)
	}
	if w := e.do("DELETE", "/v1/grants/"+handle, runnerToken, "", ""); w.Code != 404 {
		t.Fatalf("second delete %d", w.Code)
	}
	if _, ok, _ := e.st.GetVault(context.Background(), sess); ok {
		t.Fatal("vault entry survives delete")
	}
}

// deposit stores a grant for the session and returns its handle.
func (e *env) deposit(t *testing.T, session string) string {
	t.Helper()
	body, _ := json.Marshal(map[string]any{"session_id": session, "tenant": "tenant-a", "agent": "investigator",
		"refresh_token": "rt-secret-value", "expires_at": time.Now().Add(2 * time.Hour).UTC().Format(time.RFC3339)})
	w := e.do("POST", "/v1/grants", e.subject(t, consent, true), "application/json", string(body))
	var created map[string]string
	_ = json.Unmarshal(w.Body.Bytes(), &created)
	if w.Code != 201 || created["grant_handle"] == "" {
		t.Fatalf("deposit %d %s", w.Code, w.Body)
	}
	return created["grant_handle"]
}

// held reports whether the grant record and the vault entry of its session both exist.
func (e *env) held(handle, session string) bool {
	_, grant, _ := e.st.GetGrant(context.Background(), handle)
	_, vaulted, _ := e.st.GetVault(context.Background(), session)
	return grant && vaulted
}

const otherSess = "6f1d2c3e-0000-4000-8000-000000000002"

func TestGrantDeleteIsBoundToItsSession(t *testing.T) {
	e := newEnv(t)
	handle := e.deposit(t, sess)
	_ = e.st.PutExchanged(context.Background(), sess, "backend", []byte("cached"), time.Hour)

	// A session that was handed another session's handle cannot remove that grant.
	refused := []struct{ name, token, query string }{
		{"runner for another session", runnerToken, "?session_id=" + otherSess},
		{"reconciler for another session", reconToken, "?session_id=" + otherSess},
		{"empty session", runnerToken, "?session_id="},
		{"repeated session", runnerToken, "?session_id=" + sess + "&session_id=" + otherSess},
		{"own session repeated", runnerToken, "?session_id=" + sess + "&session_id=" + sess},
		{"malformed query", runnerToken, "?session_id=" + sess + "&x=%zz"},
	}
	for _, c := range refused {
		w := e.do("DELETE", "/v1/grants/"+handle+c.query, c.token, "", "")
		if w.Code != 403 || !strings.Contains(w.Body.String(), `"reason":"session"`) {
			t.Errorf("%s: %d %s", c.name, w.Code, w.Body)
		}
		if !e.held(handle, sess) {
			t.Fatalf("%s: the grant did not survive a refused delete", c.name)
		}
		if _, cached, _ := e.st.GetExchanged(context.Background(), sess, "backend"); !cached {
			t.Fatalf("%s: exchanged tokens did not survive a refused delete", c.name)
		}
	}
	if w := e.do("GET", "/v1/grants/"+handle, runnerToken, "", ""); w.Code != 200 {
		t.Fatalf("grant unusable after refused deletes: %d", w.Code)
	}
	if !strings.Contains(e.logs.String(), `"msg":"grant delete refused"`) || strings.Contains(e.logs.String(), handle) {
		t.Fatalf("refusal log: %s", e.logs)
	}

	// The tenant check still comes first and is not replaced by the assertion.
	e.kube.tenants["tenant-a"] = false
	if w := e.do("DELETE", "/v1/grants/"+handle+"?session_id="+sess, runnerToken, "", ""); w.Code != 403 || strings.Contains(w.Body.String(), `"reason":"session"`) {
		t.Fatalf("other-tenant delete %d %s", w.Code, w.Body)
	}
	e.kube.tenants["tenant-a"] = true
	if w := e.do("DELETE", "/v1/grants/"+handle+"?session_id="+sess, strangerTok, "", ""); w.Code != 403 || !e.held(handle, sess) {
		t.Fatalf("stranger delete %d", w.Code)
	}

	// The session the grant is bound to removes it, with everything it guards.
	if w := e.do("DELETE", "/v1/grants/"+handle+"?session_id="+sess, runnerToken, "", ""); w.Code != 204 {
		t.Fatalf("own delete %d %s", w.Code, w.Body)
	}
	if _, ok, _ := e.st.GetGrant(context.Background(), handle); ok {
		t.Fatal("grant record survives its own session's delete")
	}
	if _, ok, _ := e.st.GetVault(context.Background(), sess); ok {
		t.Fatal("vault entry survives its own session's delete")
	}
	if _, ok, _ := e.st.GetExchanged(context.Background(), sess, "backend"); ok {
		t.Fatal("exchanged tokens survive their own session's delete")
	}
	// An absent grant is still 404 whatever is asserted, so cleanup stays idempotent.
	for _, q := range []string{"", "?session_id=" + sess, "?session_id=" + otherSess} {
		if w := e.do("DELETE", "/v1/grants/"+handle+q, runnerToken, "", ""); w.Code != 404 {
			t.Fatalf("absent grant %q: %d", q, w.Code)
		}
	}
}

func TestGrantDeleteLegitimatePaths(t *testing.T) {
	e := newEnv(t)
	// The reconciler names the session APS itself listed beside the handle.
	handle := e.deposit(t, sess)
	var listed struct {
		Sessions []struct {
			SessionID   string `json:"session_id"`
			GrantHandle string `json:"grant_handle"`
		} `json:"sessions"`
	}
	w := e.do("GET", "/v1/sessions", reconToken, "", "")
	if err := json.Unmarshal(w.Body.Bytes(), &listed); err != nil || len(listed.Sessions) != 1 ||
		listed.Sessions[0].SessionID != sess || listed.Sessions[0].GrantHandle != handle {
		t.Fatalf("sessions %d %s", w.Code, w.Body)
	}
	if w := e.do("DELETE", "/v1/grants/"+listed.Sessions[0].GrantHandle+"?session_id="+listed.Sessions[0].SessionID, reconToken, "", ""); w.Code != 204 {
		t.Fatalf("reconciler delete %d %s", w.Code, w.Body)
	}
	if e.held(handle, sess) {
		t.Fatal("grant survives the reconciler")
	}

	// Stop removes the vault first; the grant record is then deleted by its own session.
	handle = e.deposit(t, otherSess)
	if w := e.do("POST", "/v1/sessions/"+otherSess+"/stop", runnerToken, "application/json", "{}"); w.Code != 200 {
		t.Fatalf("stop %d %s", w.Code, w.Body)
	}
	if w := e.do("GET", "/v1/grants/"+handle, runnerToken, "", ""); w.Code != 404 {
		t.Fatalf("get after stop %d", w.Code)
	}
	if w := e.do("DELETE", "/v1/grants/"+handle+"?session_id="+sess, runnerToken, "", ""); w.Code != 403 {
		t.Fatalf("another session after stop %d", w.Code)
	}
	if w := e.do("DELETE", "/v1/grants/"+handle+"?session_id="+otherSess, runnerToken, "", ""); w.Code != 204 {
		t.Fatalf("own delete after stop %d %s", w.Code, w.Body)
	}
	if _, ok, _ := e.st.GetGrant(context.Background(), handle); ok {
		t.Fatal("grant record survives")
	}

	// A runner older than the session parameter sends none and is checked by tenant alone.
	const third = "6f1d2c3e-0000-4000-8000-000000000003"
	handle = e.deposit(t, third)
	if w := e.do("DELETE", "/v1/grants/"+handle, runnerToken, "", ""); w.Code != 204 || e.held(handle, third) {
		t.Fatalf("unasserted delete %d", w.Code)
	}
}

func TestGrantsRefusals(t *testing.T) {
	e := newEnv(t)
	cases := []struct {
		name, token, body string
		code              int
	}{
		{"scope for another agent", e.subject(t, "openid delegation:client:agent-tenant-a-other", true), grantBody("tenant-a", "investigator"), 403},
		{"no may_act", e.subject(t, consent, false), grantBody("tenant-a", "investigator"), 403},
		{"agent token audience", e.idp.Sign(t, testutil.AgentClaims("j")), grantBody("tenant-a", "investigator"), 403},
		{"no token", "", grantBody("tenant-a", "investigator"), 401},
		{"bad tenant", e.subject(t, consent, true), grantBody("default", "investigator"), 400},
	}
	for _, c := range cases {
		if w := e.do("POST", "/v1/grants", c.token, "application/json", c.body); w.Code != c.code {
			t.Errorf("%s: %d, want %d", c.name, w.Code, c.code)
		}
	}
}

func TestRunnerIdentityAndGenerations(t *testing.T) {
	e := newEnv(t)
	put := func(tok string, gen int) int {
		return e.do("PUT", "/v1/sessions/"+sess+"/generation", tok, "application/json",
			`{"generation":`+string(rune('0'+gen))+`,"tenant":"tenant-a","agent":"investigator"}`).Code
	}
	if c := put("", 1); c != 401 {
		t.Fatalf("no token %d", c)
	}
	if c := put(strangerTok, 1); c != 403 {
		t.Fatalf("other SA %d", c)
	}
	if c := put(reconToken, 1); c != 403 {
		t.Fatalf("reconciler may not set generations: %d", c)
	}
	for _, g := range []int{1, 1, 2} {
		if c := put(runnerToken, g); c != 200 {
			t.Fatalf("set %d: %d", g, c)
		}
	}
	if c := put(runnerToken, 1); c != 409 {
		t.Fatalf("decrease %d", c)
	}
	e.kube.tenants["tenant-a"] = false
	if c := put(runnerToken, 3); c != 403 {
		t.Fatalf("tenant not bound %d", c)
	}
	e.kube.tenants["tenant-a"] = true

	w := e.do("GET", "/v1/sessions?older_than=0s", reconToken, "", "")
	var list struct {
		Sessions []map[string]any `json:"sessions"`
	}
	_ = json.Unmarshal(w.Body.Bytes(), &list)
	if w.Code != 200 || len(list.Sessions) != 1 {
		t.Fatalf("list %d %s", w.Code, w.Body)
	}
	s := list.Sessions[0]
	if s["session_id"] != sess || s["tenant"] != "tenant-a" || s["agent"] != "investigator" || s["generation"] != float64(2) {
		t.Fatalf("row %v", s)
	}
	if v, ok := s["grant_handle"]; !ok || v != nil || len(s) != 5 {
		t.Fatalf("row shape %v", s)
	}
	if w := e.do("GET", "/v1/sessions?older_than=15m", reconToken, "", ""); !strings.Contains(w.Body.String(), `"sessions":[]`) {
		t.Fatalf("older_than filter: %s", w.Body)
	}

	if w := e.do("POST", "/v1/sessions/"+sess+"/stop", reconToken, "application/json", "{}"); w.Code != 200 {
		t.Fatalf("stop %d", w.Code)
	}
	if st, _ := e.st.IsStopped(context.Background(), sess); !st {
		t.Fatal("not stopped")
	}
	if _, active, err := e.st.ActiveGeneration(context.Background(), sess); err != nil || active {
		t.Fatalf("stopped generation remains active: %v %v", active, err)
	}
	if w := e.do("PUT", "/v1/sessions/"+sess+"/generation", runnerToken, "application/json", `{"generation":3}`); w.Code != 409 {
		t.Fatalf("late generation after stop: %d", w.Code)
	}
	if w := e.do("GET", "/v1/sessions", reconToken, "", ""); !strings.Contains(w.Body.String(), `"sessions":[]`) {
		t.Fatalf("stopped session still holds active state: %s", w.Body)
	}
}
