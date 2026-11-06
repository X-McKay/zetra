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
	var gotHost string
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got = r.Header.Clone()
		gotHost = r.Host
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
	// The backend received everything, untouched, with Host rewritten to its own address and
	// the original Host forwarded.
	if got.Get("Authorization") != "Bearer "+token || got.Get("X-Agent-Id") != "tenant-test/conformance" || got.Get("Cookie") != "secret=1" {
		t.Fatalf("backend headers %v", got)
	}
	if gotHost != u.Host || got.Get("X-Forwarded-Host") != strings.TrimPrefix(srv.URL, "http://") {
		t.Fatalf("host %q forwarded %q", gotHost, got.Get("X-Forwarded-Host"))
	}
	line := log.line(t)
	var rec Record
	if err := json.Unmarshal([]byte(line), &rec); err != nil {
		t.Fatal(err)
	}
	if rec.Method != "POST" || rec.Path != "/kubernetes/mcp" || rec.Status != http.StatusTeapot {
		t.Fatalf("%+v", rec)
	}
	if rec.Host != strings.TrimPrefix(srv.URL, "http://") {
		t.Fatalf("record host %q", rec.Host)
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

func TestBackendKeyComparisonNeverRecordsCredential(t *testing.T) {
	key := "test-engine-static-key"
	for _, tc := range []struct {
		name, auth string
		match      bool
	}{
		{"match", "Bearer " + key, true},
		{"wrong", "Bearer wrong-static-key", false},
		{"missing", "", false},
		{"other_scheme", "Basic " + key, false},
		{"agent_jwt", "Bearer " + jwt(map[string]any{"jti": "agent-jti"}), false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("Authorization") != tc.auth {
					t.Error("authorization changed")
				}
				w.WriteHeader(http.StatusOK)
			}))
			defer backend.Close()
			u, _ := url.Parse(backend.URL)
			out := &syncBuffer{}
			srv := httptest.NewServer(HandlerWithBackendKey(u, out, key))
			defer srv.Close()
			req, _ := http.NewRequest(http.MethodPost, srv.URL+"/v1/chat/completions", nil)
			req.Header.Set("Authorization", tc.auth)
			req.Header.Set("X-Agent-Session-Id", "model-session")
			resp, err := http.DefaultClient.Do(req)
			if err != nil {
				t.Fatal(err)
			}
			_ = resp.Body.Close()
			line := out.line(t)
			var rec map[string]any
			if err := json.Unmarshal([]byte(line), &rec); err != nil {
				t.Fatal(err)
			}
			if rec["backend_key_match"] != tc.match {
				t.Fatalf("match flag: %v", rec["backend_key_match"])
			}
			if rec["path"] != "/v1/chat/completions" || rec["status"] != float64(200) {
				t.Fatal("wrong request metadata")
			}
			if strings.Contains(line, key) || strings.Contains(line, "wrong-static-key") || strings.Contains(line, "eyJ") {
				t.Fatal("credential in capture output")
			}
			if tc.match && rec["bearer"].(map[string]any)["error"] != "not_a_jwt" {
				t.Fatal("static key was mistaken for JWT")
			}
		})
	}
}

func TestRPCClassificationIsBoundedFixedAndLeavesBodyUnchanged(t *testing.T) {
	for _, tc := range []struct{ name, body, want string }{
		{"call", `{"jsonrpc":"2.0","id":"private-id","method":"tools/call","params":{"name":"pods_list_in_namespace","arguments":{"secret":"private-argument"}}}`, "tools/call"},
		{"init", `{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"clientInfo":{"name":"private-client"}}}`, "initialize"},
		{"notification", `{"jsonrpc":"2.0","method":"notifications/initialized"}`, "notifications/initialized"},
		{"list", `{"jsonrpc":"2.0","id":1,"method":"tools/list"}`, "tools/list"},
		{"unknown", `{"jsonrpc":"2.0","id":1,"method":"private-method"}`, "unclassified"},
		{"malformed", `{"jsonrpc":"2.0","method":"tools/call"`, "unclassified"},
		{"duplicate", `{"jsonrpc":"2.0","method":"tools/call","method":"initialize"}`, "unclassified"},
		{"batch", `[{"jsonrpc":"2.0","method":"tools/call"}]`, "unclassified"},
		{"version", `{"jsonrpc":"1.0","method":"tools/call"}`, "unclassified"},
		{"trailing", `{"jsonrpc":"2.0","method":"tools/call"} {}`, "unclassified"},
		{"oversize", `{"jsonrpc":"2.0","method":"tools/call","params":"` + strings.Repeat("x", 65536) + `"}`, "unclassified"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				body, err := io.ReadAll(r.Body)
				if err != nil || string(body) != tc.body {
					t.Error("forwarded bytes changed")
				}
				w.WriteHeader(200)
			}))
			defer backend.Close()
			u, _ := url.Parse(backend.URL)
			out := &syncBuffer{}
			srv := httptest.NewServer(Handler(u, out))
			defer srv.Close()
			req, _ := http.NewRequest(http.MethodPost, srv.URL+"/mcp", strings.NewReader(tc.body))
			// Exercise unknown-length/chunked forwarding too.
			req.ContentLength = -1
			resp, err := http.DefaultClient.Do(req)
			if err != nil {
				t.Fatal(err)
			}
			_ = resp.Body.Close()
			line := out.line(t)
			var record map[string]any
			if err := json.Unmarshal([]byte(line), &record); err != nil {
				t.Fatal(err)
			}
			if record["rpc_method"] != tc.want {
				t.Fatalf("classification %v, want %s", record["rpc_method"], tc.want)
			}
			for _, secret := range []string{"private-id", "private-argument", "private-client", "private-method", "pods_list_in_namespace", "arguments", "params"} {
				if strings.Contains(line, secret) {
					t.Fatalf("capture emitted body content %s", secret)
				}
			}
		})
	}
}

func TestModelBodyIsNeverClassifiedAsRPC(t *testing.T) {
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { _, _ = io.Copy(io.Discard, r.Body) }))
	defer backend.Close()
	u, _ := url.Parse(backend.URL)
	out := &syncBuffer{}
	srv := httptest.NewServer(Handler(u, out))
	defer srv.Close()
	resp, err := http.Post(srv.URL+"/v1/chat/completions", "application/json", strings.NewReader(`{"jsonrpc":"2.0","method":"tools/call"}`))
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if strings.Contains(out.line(t), "rpc_method") {
		t.Fatal("model body was inspected")
	}
}

type countingBody struct {
	reads  int
	closed bool
}

func (b *countingBody) Read(p []byte) (int, error) {
	b.reads++
	copy(p, "abc")
	return 3, io.ErrUnexpectedEOF
}
func (b *countingBody) Close() error { b.closed = true; return nil }

func TestRPCObserverNeverReadsAheadAndPreservesErrorsAndClose(t *testing.T) {
	source := &countingBody{}
	body := &rpcBody{ReadCloser: source, length: -1}
	if body.method() != "unclassified" || source.reads != 0 {
		t.Fatal("classification read ahead")
	}
	p := make([]byte, 8)
	n, err := body.Read(p)
	if n != 3 || err != io.ErrUnexpectedEOF || string(p[:n]) != "abc" || source.reads != 1 {
		t.Fatal("body read changed")
	}
	if body.method() != "unclassified" {
		t.Fatal("failed read was accepted")
	}
	if body.Close() != nil || !source.closed {
		t.Fatal("close not forwarded")
	}
}

func TestRPCObserverBoundsMemoryAndRequiresCompleteBody(t *testing.T) {
	raw := `{"jsonrpc":"2.0","method":"tools/call"}`
	body := &rpcBody{ReadCloser: io.NopCloser(strings.NewReader(raw)), length: int64(len(raw) + 1)}
	p := make([]byte, len(raw))
	_, _ = body.Read(p)
	if body.method() != "unclassified" {
		t.Fatal("incomplete request accepted")
	}
	_, _ = body.Read(p) // EOF cannot satisfy a declared length that was never read.
	if body.method() != "unclassified" {
		t.Fatal("early EOF accepted")
	}
	large := &rpcBody{ReadCloser: io.NopCloser(strings.NewReader(strings.Repeat("x", 3*maxRPCBytes))), length: -1}
	if _, err := io.Copy(io.Discard, large); err != nil {
		t.Fatal(err)
	}
	if len(large.prefix) > maxRPCBytes || cap(large.prefix) > maxRPCBytes || large.total != 3*maxRPCBytes || large.method() != "unclassified" {
		t.Fatal("observation memory/stream bound not preserved")
	}
}
