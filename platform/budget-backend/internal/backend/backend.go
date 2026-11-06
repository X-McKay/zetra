// Package backend is a deterministic, bounded conformance model, not a Qwen proxy.
package backend

import (
	"crypto/subtle"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"
)

const Model = "conformance-byte-v1"
const MaxInput = 200
const MaxOutput = 100
const MaxTotal = MaxInput + MaxOutput

var uuid = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)
var trace = regexp.MustCompile(`^00-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$`)

type message struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}
type chatRequest struct {
	Model       string    `json:"model"`
	Messages    []message `json:"messages"`
	MaxTokens   int       `json:"max_tokens"`
	N           *int      `json:"n"`
	Stream      bool      `json:"stream"`
	Temperature float64   `json:"temperature"`
}
type server struct {
	key    string
	agents []string
	log    *slog.Logger
}

func New(key string, agents []string, log *slog.Logger) http.Handler {
	s := &server{key: key, agents: append([]string(nil), agents...), log: log}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(200) })
	mux.HandleFunc("POST /v1/chat/completions", s.chat)
	return mux
}
func reply(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
func reject(w http.ResponseWriter, status int) {
	reply(w, status, map[string]any{"error": map[string]string{"type": "invalid_request_error", "message": "conformance request refused"}})
}
func (s *server) chat(w http.ResponseWriter, r *http.Request) {
	if s.key == "" || subtle.ConstantTimeCompare([]byte(r.Header.Get("Authorization")), []byte("Bearer "+s.key)) != 1 {
		reject(w, 401)
		return
	}
	agent, session, decision := r.Header.Get("x-agent-id"), r.Header.Get("x-agent-session-id"), r.Header.Get("x-agent-decision-id")
	if !slices.Contains(s.agents, agent) || !uuid.MatchString(session) || !uuid.MatchString(decision) {
		reject(w, 403)
		return
	}
	var req chatRequest
	d := json.NewDecoder(http.MaxBytesReader(w, r.Body, 8192))
	d.DisallowUnknownFields()
	if d.Decode(&req) != nil || d.Decode(&struct{}{}) != io.EOF {
		reject(w, 400)
		return
	}
	if req.Model != Model || len(req.Messages) == 0 || len(req.Messages) > 16 || req.MaxTokens < 1 || req.MaxTokens > MaxOutput || req.Stream || req.N != nil && *req.N != 1 || req.Temperature != 0 {
		reject(w, 400)
		return
	}
	// This model's tokenizer is exactly one token per UTF-8 byte in role and
	// content, in message order. No hidden template, BOS/EOS or model execution.
	input := 0
	for _, m := range req.Messages {
		if !slices.Contains([]string{"system", "user", "assistant"}, m.Role) {
			reject(w, 400)
			return
		}
		input += len([]byte(m.Role)) + len([]byte(m.Content))
	}
	if input > MaxInput {
		reject(w, 400)
		return
	}
	delay := 0
	if value := r.Header.Get("x-conformance-hold-ms"); value != "" {
		var err error
		delay, err = strconv.Atoi(value)
		if err != nil || delay < 0 || delay > 10000 {
			reject(w, 400)
			return
		}
	}
	started := time.Now().UTC()
	if delay > 0 {
		timer := time.NewTimer(time.Duration(delay) * time.Millisecond)
		defer timer.Stop()
		select {
		case <-r.Context().Done():
			return
		case <-timer.C:
		}
	}
	output := strings.Repeat("x", req.MaxTokens)
	usage := map[string]int{"prompt_tokens": input, "completion_tokens": len([]byte(output)), "total_tokens": input + len([]byte(output))}
	responding := time.Now().UTC() // before writing: a conservative response-to-settlement interval
	reply(w, 200, map[string]any{"id": "chatcmpl-" + decision, "object": "chat.completion", "created": started.Unix(), "model": Model, "choices": []any{map[string]any{"index": 0, "message": message{Role: "assistant", Content: output}, "finish_reason": "length"}}, "usage": usage})
	if s.log != nil {
		traceID := ""
		if match := trace.FindStringSubmatch(r.Header.Get("traceparent")); match != nil {
			traceID = match[1]
		}
		s.log.Info("budget backend response", "session_id", session, "agent_id", agent, "decision_id", decision, "trace_id", traceID, "method", r.Method, "path", r.URL.Path, "status", 200, "received_at", started.Format(time.RFC3339Nano), "response_at", responding.Format(time.RFC3339Nano), "input_tokens", input, "output_tokens", len(output), "total_tokens", input+len(output), "max_total_tokens", MaxTotal)
	}
}
