package decision

import (
	"encoding/json"
	"io"
	"strconv"
	"strings"
	"sync"
	"time"

	authv3 "github.com/envoyproxy/go-control-plane/envoy/service/auth/v3"

	"github.com/X-McKay/zetra/platform/aps/internal/policy"
)

// Logger writes the decision log of 13.2: one JSON line per check. It never
// writes a token, an Authorization header or a request body.
type Logger struct {
	mu sync.Mutex
	W  io.Writer
}

func NewLogger(w io.Writer) *Logger { return &Logger{W: w} }

func traceID(tp string) any {
	parts := strings.Split(tp, "-")
	if len(parts) == 4 && len(parts[1]) == 32 {
		return parts[1]
	}
	return nil
}

func nz(s string) any {
	if s == "" {
		return nil
	}
	return s
}

func (l *Logger) Decision(decision string, req *authv3.CheckRequest, r *result, elapsed time.Duration) {
	if l == nil || l.W == nil {
		return
	}
	http := req.GetAttributes().GetRequest().GetHttp()
	path, _, _ := strings.Cut(http.GetPath(), "?")
	e := map[string]any{
		"ts": time.Now().UTC().Format("2006-01-02T15:04:05.000Z"), "decision_id": decision, "reason": r.reason,
		"tenant": nil, "agent_id": nil, "spiffe_id": nil, "token_jti": nil,
		"session_id": nil, "generation": nil, "release": nil, "policy_digest": nil, "pod": nil,
		"route": nz(r.route), "backend": nz(r.backend), "path": nz(path),
		"method": nz(r.rpcMethod), "tool": nz(r.tool), "model": nil,
		"delegation": nz(r.delegation), "principal": nz(r.principal), "exchange": nil,
		"source_ip": nz(req.GetAttributes().GetSource().GetAddress().GetSocketAddress().GetAddress()),
		"trace_id":  traceID(http.GetHeaders()["traceparent"]), "ms": elapsed.Milliseconds(),
	}
	if r.claims != nil {
		e["tenant"], e["agent_id"], e["spiffe_id"], e["token_jti"] = nz(r.claims.Tenant), nz(r.claims.AgentID), nz(r.claims.SpiffeID), nz(r.claims.JTI)
	}
	if r.pod != nil {
		lb := r.pod.GetLabels()
		e["session_id"], e["release"], e["pod"] = nz(lb[policy.LabelSession]), nz(lb[policy.LabelRelease]), nz(r.pod.GetName())
		if g, err := strconv.Atoi(lb[policy.LabelGeneration]); err == nil {
			e["generation"] = g
		}
		e["policy_digest"] = nz(r.pod.GetAnnotations()[policy.AnnotationDigest])
	}
	if r.exchange != nil {
		e["exchange"] = map[string]any{"cached": r.exchange.Cached, "ms": r.exchange.Duration.Milliseconds(), "upstream_jti": nz(r.exchange.UpstreamJTI)}
	}
	b, err := json.Marshal(e)
	if err != nil {
		return
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	_, _ = l.W.Write(append(b, '\n'))
}
