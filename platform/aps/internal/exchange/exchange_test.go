package exchange

import (
	"context"
	"crypto/rand"
	"errors"
	"net/http"
	"net/http/httptest"
	"net/url"
	"sync"
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
	resource = "https://mcp.example.com/kubernetes/mcp"
	sess     = "6f1d2c3e-0000-4000-8000-000000000001"
)

func TestKeycloakRetryPolicy(t *testing.T) {
	cases := []struct {
		name   string
		status []int
		calls  int32
		ok     bool
	}{
		{"4xx no retry", []int{400}, 1, false},
		{"5xx retried once then ok", []int{503, 200}, 2, true},
		{"5xx twice fails", []int{500, 502}, 2, false},
	}
	for _, c := range cases {
		var n atomic.Int32
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			i := n.Add(1) - 1
			w.WriteHeader(c.status[i])
			if c.status[i] == 200 {
				_, _ = w.Write([]byte(`{"access_token":"t","expires_in":300}`))
			} else {
				_, _ = w.Write([]byte(`{"error":"invalid_grant"}`))
			}
		}))
		k := &Keycloak{TokenEndpoint: srv.URL, ClientID: "agent-policy-service", ClientSecret: "s", HTTP: srv.Client()}
		_, err := k.Agent(context.Background(), "subject", resource)
		if (err == nil) != c.ok || n.Load() != c.calls {
			t.Errorf("%s: err=%v calls=%d", c.name, err, n.Load())
		}
		srv.Close()
	}
}

func TestKeycloakTimeoutRetriedOnce(t *testing.T) {
	var n atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if n.Add(1) == 1 {
			select {
			case <-r.Context().Done():
			case <-time.After(time.Second):
			}
			return
		}
		_, _ = w.Write([]byte(`{"access_token":"t","expires_in":300}`))
	}))
	defer srv.Close()
	cl := srv.Client()
	cl.Timeout = 200 * time.Millisecond
	k := &Keycloak{TokenEndpoint: srv.URL, HTTP: cl}
	if _, err := k.Agent(context.Background(), "s", resource); err != nil || n.Load() != 2 {
		t.Fatalf("err=%v calls=%d", err, n.Load())
	}
}

func TestKeycloakRequestShapes(t *testing.T) {
	var forms []url.Values
	var basics []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = r.ParseForm()
		forms = append(forms, r.PostForm)
		u, p, _ := r.BasicAuth()
		basics = append(basics, u+":"+p)
		_, _ = w.Write([]byte(`{"access_token":"t","expires_in":300}`))
	}))
	defer srv.Close()
	k := &Keycloak{TokenEndpoint: srv.URL, ClientID: "agent-policy-service", ClientSecret: "sec", HTTP: srv.Client()}
	ctx := context.Background()
	_, _ = k.Delegated(ctx, DelegatedRequest{ClientID: "agent-tenant-a-investigator", ClientAssertion: "sa", SubjectToken: "user", ActorToken: "agent", Scope: "mcp:kubernetes"})
	_, _ = k.Agent(ctx, "agent", resource)
	_, _ = k.Refresh(ctx, "rt")

	d := forms[0]
	for k, v := range map[string]string{
		"grant_type": policy.GrantTokenExchange, "client_id": "agent-tenant-a-investigator",
		"client_assertion_type": policy.AssertionTypeJWT, "client_assertion": "sa", "subject_token": "user",
		"actor_token": "agent", "actor_token_type": policy.TokenTypeAccess, "scope": "mcp:kubernetes",
	} {
		if d.Get(k) != v {
			t.Errorf("delegated %s = %q", k, d.Get(k))
		}
	}
	if basics[0] != ":" {
		t.Error("delegated mode must not use the APS client secret")
	}
	a := forms[1]
	if a.Get("subject_token") != "agent" || a.Get("audience") != resource || a.Has("actor_token") || basics[1] != "agent-policy-service:sec" {
		t.Errorf("agent mode form %v basic %s", a, basics[1])
	}
	r := forms[2]
	if r.Get("grant_type") != "refresh_token" || r.Get("client_id") != "agent-cli" || r.Get("refresh_token") != "rt" || basics[2] != ":" {
		t.Errorf("refresh form %v", r)
	}
}

type fakeAdapter struct {
	mu        sync.Mutex
	delegated []DelegatedRequest
	agent     int
	refresh   int
	token     func() string
	refreshOK bool
	block     chan struct{}
}

func (f *fakeAdapter) Delegated(_ context.Context, r DelegatedRequest) (*TokenResponse, error) {
	if f.block != nil {
		<-f.block
	}
	f.mu.Lock()
	f.delegated = append(f.delegated, r)
	f.mu.Unlock()
	return &TokenResponse{AccessToken: f.token(), ExpiresIn: 300}, nil
}

func (f *fakeAdapter) Agent(context.Context, string, string) (*TokenResponse, error) {
	f.mu.Lock()
	f.agent++
	f.mu.Unlock()
	return &TokenResponse{AccessToken: f.token(), ExpiresIn: 300}, nil
}

func (f *fakeAdapter) Refresh(context.Context, string) (*TokenResponse, error) {
	f.mu.Lock()
	f.refresh++
	f.mu.Unlock()
	if !f.refreshOK {
		return nil, &Error{Class: "4xx", Status: 400}
	}
	return &TokenResponse{AccessToken: "refreshed-user-token", RefreshToken: "rt2", ExpiresIn: 300}, nil
}

type fakeKube struct{ kube.API }

func (fakeKube) AgentToken(_ context.Context, ns, sa string, pod *corev1.Pod, aud string, secs int64) (string, error) {
	if sa != "agent-investigator" || pod.Name != "s-1-g1" || aud != testutil.Issuer || secs != 300 {
		return "", errors.New("unexpected token request")
	}
	return "sa-assertion", nil
}

func setup(t *testing.T, ad *fakeAdapter) (*Exchanger, *vault.Entries) {
	idp := testutil.NewIdP(t)
	if ad.token == nil {
		ad.token = func() string {
			return idp.Sign(t, map[string]any{"sub": "alice", "aud": []string{resource}, "act": map[string]any{"sub": "9b2c"}, "jti": "d41a"})
		}
	}
	key := make([]byte, 32)
	_, _ = rand.Read(key)
	v, _ := vault.New(key)
	st := store.NewMemory()
	en := &vault.Entries{Store: st, Vault: v, Now: time.Now}
	return &Exchanger{
		Adapter: ad, Store: st, Vault: v, Entries: en, Verifier: jwtverify.New(testutil.Issuer, idp.Server.URL, nil),
		Kube: fakeKube{}, Issuer: testutil.Issuer, Now: time.Now,
	}, en
}

var pod = &corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "s-1-g1", Namespace: "tenant-a", UID: "u1",
	Labels: map[string]string{policy.LabelAgent: "investigator"}}}

func grant(t *testing.T, en *vault.Entries, accessLeft time.Duration, refresh string) {
	_, err := en.Create(context.Background(), sess, &vault.Entry{
		AccessToken: "user-token", RefreshToken: refresh, ExpiresAt: time.Now().Add(time.Hour),
		AccessExpiresAt: time.Now().Add(accessLeft), Tenant: "tenant-a", Agent: "investigator", PrincipalSub: "alice",
	})
	if err != nil {
		t.Fatal(err)
	}
}

func TestDelegatedExchangeSingleFlightAndCache(t *testing.T) {
	ad := &fakeAdapter{block: make(chan struct{})}
	x, en := setup(t, ad)
	grant(t, en, 5*time.Minute, "")
	var wg sync.WaitGroup
	results := make([]*policy.Exchanged, 10)
	for i := range results {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			r, err := x.Exchange(context.Background(), "on-behalf-of", sess, "kubernetes", resource, "agent-token", pod)
			if err != nil {
				t.Error(err)
			}
			results[i] = r
		}(i)
	}
	time.Sleep(50 * time.Millisecond)
	close(ad.block)
	wg.Wait()
	if len(ad.delegated) != 1 {
		t.Fatalf("IdP calls = %d, want 1", len(ad.delegated))
	}
	r := ad.delegated[0]
	if r.ClientID != "agent-tenant-a-investigator" || r.ClientAssertion != "sa-assertion" || r.SubjectToken != "user-token" ||
		r.ActorToken != "agent-token" || r.Scope != "mcp:kubernetes" {
		t.Fatalf("delegated request %+v", r)
	}
	again, err := x.Exchange(context.Background(), "on-behalf-of", sess, "kubernetes", resource, "agent-token", pod)
	if err != nil || !again.Cached || again.Token != results[0].Token || again.UpstreamJTI != "d41a" || len(ad.delegated) != 1 {
		t.Fatalf("cache reuse: %+v %v", again, err)
	}
}

func TestExchangeValidation(t *testing.T) {
	idp := testutil.NewIdP(t)
	for name, claims := range map[string]map[string]any{
		"wrong audience": {"sub": "alice", "aud": []string{"https://mcp.example.com/github/mcp"}, "act": map[string]any{"sub": "9b2c"}},
		"two audiences":  {"sub": "alice", "aud": []string{resource, "other"}, "act": map[string]any{"sub": "9b2c"}},
		"no act":         {"sub": "alice", "aud": []string{resource}},
	} {
		ad := &fakeAdapter{}
		x, en := setup(t, ad)
		x.Verifier = jwtverify.New(testutil.Issuer, idp.Server.URL, nil)
		ad.token = func() string { return idp.Sign(t, claims) }
		grant(t, en, 5*time.Minute, "")
		if _, err := x.Exchange(context.Background(), "on-behalf-of", sess, "kubernetes", resource, "a", pod); err == nil {
			t.Errorf("%s: accepted", name)
		}
		if _, ok, _ := x.Store.GetExchanged(context.Background(), sess, "kubernetes|on-behalf-of"); ok {
			t.Errorf("%s: invalid token cached", name)
		}
	}
}

func TestAgentModeNeedsNoAct(t *testing.T) {
	idp := testutil.NewIdP(t)
	ad := &fakeAdapter{}
	x, _ := setup(t, ad)
	x.Verifier = jwtverify.New(testutil.Issuer, idp.Server.URL, nil)
	ad.token = func() string { return idp.Sign(t, map[string]any{"sub": "9b2c", "aud": []string{resource}}) }
	if _, err := x.Exchange(context.Background(), "agent", sess, "kubernetes", resource, "a", pod); err != nil || ad.agent != 1 {
		t.Fatalf("agent mode: %v %d", err, ad.agent)
	}
}

func TestRefreshWhenUnder120s(t *testing.T) {
	ad := &fakeAdapter{refreshOK: true}
	x, en := setup(t, ad)
	grant(t, en, 60*time.Second, "rt1")
	if _, err := x.Exchange(context.Background(), "on-behalf-of", sess, "kubernetes", resource, "a", pod); err != nil {
		t.Fatal(err)
	}
	if ad.refresh != 1 || ad.delegated[0].SubjectToken != "refreshed-user-token" {
		t.Fatalf("refresh %d subject %q", ad.refresh, ad.delegated[0].SubjectToken)
	}
	e, _, _ := en.Load(context.Background(), sess)
	if e.AccessToken != "refreshed-user-token" || e.RefreshToken != "rt2" {
		t.Fatal("vault not updated")
	}
}

func TestRefreshFailureMarksExpired(t *testing.T) {
	ad := &fakeAdapter{refreshOK: false}
	x, en := setup(t, ad)
	grant(t, en, 60*time.Second, "rt1")
	_, err := x.Exchange(context.Background(), "on-behalf-of", sess, "kubernetes", resource, "a", pod)
	if !errors.Is(err, policy.ErrDelegationRequired) {
		t.Fatalf("err = %v", err)
	}
	if ok, _ := en.Usable(context.Background(), sess); ok {
		t.Fatal("entry still usable")
	}
	if len(ad.delegated) != 0 {
		t.Fatal("exchange attempted after failed refresh")
	}
}

func TestNoGrantIsDelegationRequired(t *testing.T) {
	x, _ := setup(t, &fakeAdapter{})
	_, err := x.Exchange(context.Background(), "on-behalf-of", sess, "kubernetes", resource, "a", pod)
	if !errors.Is(err, policy.ErrDelegationRequired) {
		t.Fatal(err)
	}
}
