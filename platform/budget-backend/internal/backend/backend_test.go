package backend

import (
	"bytes"
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func request(t *testing.T, body, key string) (*httptest.ResponseRecorder, string) {
	t.Helper()
	var logs bytes.Buffer
	h := New("backend-secret", []string{"tenant-test/conformance-budget-day"}, slog.New(slog.NewJSONHandler(&logs, nil)))
	r := httptest.NewRequest("POST", "/v1/chat/completions", strings.NewReader(body))
	r.Header.Set("Authorization", "Bearer "+key)
	r.Header.Set("x-agent-id", "tenant-test/conformance-budget-day")
	r.Header.Set("x-agent-session-id", "6f1d2c3e-0000-4000-8000-000000000001")
	r.Header.Set("x-agent-decision-id", "6f1d2c3e-0000-4000-8000-000000000002")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	return w, logs.String()
}
func TestComputedByteUsage(t *testing.T) {
	w, logs := request(t, `{"model":"conformance-byte-v1","messages":[{"role":"user","content":"`+strings.Repeat("x", 46)+`"}],"max_tokens":50}`, "backend-secret")
	if w.Code != 200 {
		t.Fatalf("response %d %s", w.Code, w.Body.String())
	}
	var got struct {
		Usage struct {
			Prompt     int `json:"prompt_tokens"`
			Completion int `json:"completion_tokens"`
			Total      int `json:"total_tokens"`
		}
		Choices []struct{ Message struct{ Content string } }
	}
	if json.Unmarshal(w.Body.Bytes(), &got) != nil || got.Usage.Prompt != 50 || got.Usage.Completion != 50 || got.Usage.Total != 100 || len(got.Choices) != 1 || len(got.Choices[0].Message.Content) != 50 {
		t.Fatalf("computed usage: %s", w.Body.String())
	}
	if strings.Contains(logs, "backend-secret") || strings.Contains(logs, strings.Repeat("x", 46)) || !strings.Contains(logs, `"total_tokens":100`) {
		t.Fatalf("unsafe/missing evidence: %s", logs)
	}
}
func TestHardBoundAndUnsupportedAmplification(t *testing.T) {
	for _, tc := range []struct {
		field string
		want  int
	}{
		{`"max_tokens":100`, 200}, {`"max_tokens":101`, 400}, {`"max_tokens":0`, 400}, {`"max_tokens":-1`, 400},
		{`"max_tokens":50,"n":2`, 400}, {`"max_tokens":50,"n":0`, 400}, {`"max_tokens":50,"n":1`, 200},
		{`"max_tokens":50,"best_of":2`, 400}, {`"max_completion_tokens":50`, 400}, {`"max_tokens":50,"stream":true`, 400},
		{`"max_tokens":50,"usage":{"total_tokens":0}`, 400}, {`"max_tokens":50,"temperature":1`, 400},
	} {
		t.Run(tc.field, func(t *testing.T) {
			w, _ := request(t, `{"model":"conformance-byte-v1","messages":[{"role":"user","content":"x"}],`+tc.field+`}`, "backend-secret")
			if w.Code != tc.want {
				t.Fatalf("status %d want %d", w.Code, tc.want)
			}
		})
	}
	for _, content := range []string{strings.Repeat("x", 197), strings.Repeat("é", 99), strings.Repeat("x", 8192)} {
		w, _ := request(t, `{"model":"conformance-byte-v1","messages":[{"role":"user","content":"`+content+`"}],"max_tokens":100}`, "backend-secret")
		if w.Code != 400 {
			t.Fatalf("oversize status %d", w.Code)
		}
	}
	w, _ := request(t, `{"model":"conformance-byte-v1","messages":[{"role":"user","content":"`+strings.Repeat("x", 196)+`"}],"max_tokens":100}`, "backend-secret")
	if w.Code != 200 {
		t.Fatalf("300 bound status %d", w.Code)
	}
}
func TestAuthenticationAndIdentity(t *testing.T) {
	w, logs := request(t, `{"untrusted":"do-not-log-me"}`, "wrong-secret")
	if w.Code != 401 || strings.Contains(logs, "wrong-secret") || strings.Contains(logs, "do-not-log-me") {
		t.Fatal("credential handling")
	}
	h := New("backend-secret", []string{"tenant-test/conformance-budget-day"}, nil)
	r := httptest.NewRequest("POST", "/v1/chat/completions", nil)
	r.Header.Set("Authorization", "Bearer backend-secret")
	w = httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusForbidden {
		t.Fatalf("missing identity %d", w.Code)
	}
}
