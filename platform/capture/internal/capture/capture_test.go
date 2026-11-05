package capture

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"
)

// syncBuffer is the log sink: the record is written after the response has been served,
// so a test reads it once a full line is there.
type syncBuffer struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (s *syncBuffer) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.Write(p)
}

func (s *syncBuffer) line(t *testing.T) string {
	t.Helper()
	for i := 0; i < 100; i++ {
		s.mu.Lock()
		text := s.b.String()
		s.mu.Unlock()
		if strings.HasSuffix(text, "\n") {
			return strings.TrimSpace(text)
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("no record written")
	return ""
}

func jwt(claims map[string]any) string {
	payload, _ := json.Marshal(claims)
	seg := func(b []byte) string { return base64.RawURLEncoding.EncodeToString(b) }
	return seg([]byte(`{"alg":"RS256"}`)) + "." + seg(payload) + "." + seg([]byte("sig"))
}

func TestClaimsReportIdentityNeverTheToken(t *testing.T) {
	token := jwt(map[string]any{
		"sub": "agent-sa", "act": map[string]any{"sub": "agent"}, "aud": "https://mcp/kubernetes/mcp",
		"azp": "agent-policy-service", "jti": "trrtte:1", "exp": 1700000000,
	})
	b := Claims(token)
	if b.Sub != "agent-sa" || b.ActSub != "agent" || b.Azp != "agent-policy-service" || b.JTI != "trrtte:1" || b.Exp != 1700000000 {
		t.Fatalf("%+v", b)
	}
	if len(b.Aud) != 1 || b.Aud[0] != "https://mcp/kubernetes/mcp" {
		t.Fatalf("aud %v", b.Aud)
	}
	multi := Claims(jwt(map[string]any{"aud": []string{"a", "b"}}))
	if len(multi.Aud) != 2 {
		t.Fatalf("aud %v", multi.Aud)
	}
	if Claims("opaque").Error != "not_a_jwt" || Claims("a.b.c").Error != "not_a_jwt" {
		t.Fatal("non-JWT bearers must be reported as such")
	}
}

func TestHandlerForwardsUnchangedAndRecordsWhatArrived(t *testing.T) {
	var got http.Header
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got = r.Header.Clone()
		w.WriteHeader(http.StatusTeapot)
		_, _ = w.Write([]byte("ok"))
	}))
	defer backend.Close()
	u, _ := url.Parse(backend.URL)
	log := &syncBuffer{}
	srv := httptest.NewServer(Handler(u, log))
	defer srv.Close()

	token := jwt(map[string]any{"sub": "agent-sa", "jti": "trrtte:2", "aud": []string{"x"}})
	req, _ := http.NewRequest(http.MethodPost, srv.URL+"/kubernetes/mcp", strings.NewReader(`{"method":"tools/call"}`))
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("X-Agent-Id", "tenant-test/conformance")
	req.Header.Set("X-Agent-Session-Id", "s1")
	req.Header.Set("Cookie", "secret=1")
	resp, err := http.DefaultClient.Do(req)
	if err != nil || resp.StatusCode != http.StatusTeapot {
		t.Fatalf("resp %v %v", resp, err)
	}
	_, _ = io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	// The backend received everything, untouched.
	if got.Get("Authorization") != "Bearer "+token || got.Get("X-Agent-Id") != "tenant-test/conformance" || got.Get("Cookie") != "secret=1" {
		t.Fatalf("backend headers %v", got)
	}
	line := log.line(t)
	var rec Record
	if err := json.Unmarshal([]byte(line), &rec); err != nil {
		t.Fatal(err)
	}
	if rec.Method != "POST" || rec.Path != "/kubernetes/mcp" || rec.Status != http.StatusTeapot {
		t.Fatalf("%+v", rec)
	}
	if rec.Agent["x-agent-id"] != "tenant-test/conformance" || rec.Agent["x-agent-session-id"] != "s1" || len(rec.Agent) != 2 {
		t.Fatalf("x-agent headers %v", rec.Agent)
	}
	if rec.Bearer == nil || rec.Bearer.Sub != "agent-sa" || rec.Bearer.JTI != "trrtte:2" {
		t.Fatalf("bearer %+v", rec.Bearer)
	}
	for _, forbidden := range []string{token, "secret=1", "Cookie", "tools/call"} {
		if strings.Contains(line, forbidden) {
			t.Fatalf("record leaks %q", forbidden)
		}
	}
	if strings.Contains(line, "eyJ") {
		t.Fatal("record contains a JWT shape")
	}
}

func TestHandlerRecordsRequestsWithoutBearer(t *testing.T) {
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	defer backend.Close()
	u, _ := url.Parse(backend.URL)
	log := &syncBuffer{}
	srv := httptest.NewServer(Handler(u, log))
	defer srv.Close()
	resp, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	_, _ = io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	var rec Record
	_ = json.Unmarshal([]byte(log.line(t)), &rec)
	if rec.Bearer != nil || rec.Path != "/healthz" || rec.Status != 200 {
		t.Fatalf("%+v", rec)
	}
}
