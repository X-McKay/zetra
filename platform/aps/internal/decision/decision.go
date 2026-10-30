// Package decision is the APS decision path: the twelve steps of 9.3 and the
// response of 9.6, served as an Envoy ext_authz gRPC service.
package decision

import (
	"context"
	"encoding/json"
	"errors"
	"strconv"
	"strings"
	"time"

	corev3 "github.com/envoyproxy/go-control-plane/envoy/config/core/v3"
	authv3 "github.com/envoyproxy/go-control-plane/envoy/service/auth/v3"
	typev3 "github.com/envoyproxy/go-control-plane/envoy/type/v3"
	"github.com/google/uuid"
	"google.golang.org/genproto/googleapis/rpc/status"
	"google.golang.org/grpc/codes"
	"google.golang.org/protobuf/types/known/structpb"
	corev1 "k8s.io/api/core/v1"

	"github.com/X-McKay/zetra/platform/aps/internal/metrics"
	"github.com/X-McKay/zetra/platform/aps/internal/policy"
)

// AgentHeaders is every x-agent-* header the platform defines (3.5). The
// response removes each one it does not set, so a client-supplied value never
// reaches an upstream.
var AgentHeaders = []string{
	"x-agent-id", "x-agent-spiffe-id", "x-agent-tenant", "x-agent-session-id", "x-agent-generation",
	"x-agent-release", "x-agent-policy", "x-agent-principal", "x-agent-decision-id", "x-agent-decision-reason",
}

const (
	attestationMaxAge = 90 * time.Second
	checkTimeout      = 1800 * time.Millisecond // under the gateway's 2 s
)

// Deps are injected collaborators; each is a function so tests can fake it.
// A nil function panics, which the decision path turns into deny.internal.
type Deps struct {
	Verify    func(ctx context.Context, bearer string) (*policy.AgentClaims, error)
	PodByIP   func(ip string) (*corev1.Pod, int)
	PinOK     func(ctx context.Context, jti, podUID string, exp time.Time) (bool, error)
	ActiveGen func(ctx context.Context, session string) (int, bool, error)
	Stopped   func(ctx context.Context, session string) (bool, error)
	ReleaseOK func(ns, name, digest string) (bool, error) // ready, digest match, agent enabled
	Bundle    func(ns, release, digest string) (*policy.Bundle, error)
	Reserve   func(ctx context.Context, session, agentID, backend, decision string, b *policy.Bundle) (int, error)
	HasGrant  func(ctx context.Context, session string) (bool, error)
	Principal func(ctx context.Context, session string) (string, error)
	Exchange  func(ctx context.Context, mode, session, backend, resource, actor string, pod *corev1.Pod) (*policy.Exchanged, error)
	Now       func() time.Time
}

type Server struct {
	authv3.UnimplementedAuthorizationServer
	D       Deps
	Log     *Logger
	Metrics *metrics.Metrics
}

func (s *Server) now() time.Time {
	if s.D.Now != nil {
		return s.D.Now()
	}
	return time.Now()
}

func (s *Server) Check(ctx context.Context, req *authv3.CheckRequest) (*authv3.CheckResponse, error) {
	start := time.Now()
	ctx, cancel := context.WithTimeout(ctx, checkTimeout)
	defer cancel()
	decision := uuid.NewString()
	res := s.decide(ctx, req, decision)
	if res.reason == "allow" && ctx.Err() != nil {
		res.reason, res.upstream = "deny.internal", ""
	}
	elapsed := time.Since(start)
	s.Log.Decision(decision, req, &res, elapsed)
	tenant := ""
	if res.claims != nil {
		tenant = res.claims.Tenant
	}
	s.Metrics.Decision(res.reason, res.route, tenant, elapsed)
	return res.toProto(decision), nil
}

type result struct {
	reason     string
	claims     *policy.AgentClaims
	pod        *corev1.Pod
	bundle     *policy.Bundle
	route      string
	backend    string
	rpcMethod  string
	tool       string
	delegation string
	reserved   int
	upstream   string // Authorization value for MCP; empty for inference; never logged
	principal  string
	exchange   *policy.Exchanged
}

// attested reports whether the node attestor has recorded host-side policy for
// this pod within attestationMaxAge (annotation value "<RFC3339>;<chain>").
func attested(pod *corev1.Pod, now time.Time) bool {
	v := pod.GetAnnotations()[policy.AnnotationAttest]
	ts, _, _ := strings.Cut(v, ";")
	t, err := time.Parse(time.RFC3339, ts)
	return err == nil && now.Sub(t) <= attestationMaxAge && now.Sub(t) >= -attestationMaxAge
}

func bearerOf(h string) string {
	if len(h) > 7 && strings.EqualFold(h[:7], "bearer ") {
		return strings.TrimSpace(h[7:])
	}
	return ""
}

func (s *Server) decide(ctx context.Context, req *authv3.CheckRequest, decision string) (r result) {
	defer func() {
		if rec := recover(); rec != nil {
			r.reason, r.upstream, r.principal = "deny.internal", "", ""
		}
	}()
	http := req.GetAttributes().GetRequest().GetHttp()
	ext := req.GetAttributes().GetContextExtensions()
	route, backend := ext["route"], ext["backend"]
	r.route, r.backend = route, backend

	// 1. token
	bearer := bearerOf(http.GetHeaders()["authorization"])
	if bearer == "" {
		r.reason = "deny.token_invalid"
		return
	}
	claims, err := s.D.Verify(ctx, bearer)
	if err != nil || claims == nil {
		r.reason = "deny.token_invalid"
		return
	}
	if claims.ClientType != "agent" || claims.AgentID == "" || claims.Tenant == "" || claims.JTI == "" {
		r.reason, r.claims = "deny.token_not_agent", claims
		return
	}
	r.claims = claims

	// 2. pod by source address
	ip := req.GetAttributes().GetSource().GetAddress().GetSocketAddress().GetAddress()
	pod, n := s.D.PodByIP(ip)
	if n != 1 || pod == nil {
		r.reason = "deny.pod_unknown"
		return
	}
	r.pod = pod
	labels := pod.GetLabels()

	// 3. pod and token agree on agent and tenant
	agentName := claims.AgentID[strings.LastIndex(claims.AgentID, "/")+1:]
	if labels[policy.LabelAgent] != agentName || pod.GetNamespace() != claims.Tenant {
		r.reason = "deny.pod_agent_mismatch"
		return
	}

	// 4. the token is pinned to the pod it was issued for
	if ok, err := s.D.PinOK(ctx, claims.JTI, string(pod.GetUID()), claims.Exp); err != nil {
		r.reason = "deny.internal"
		return
	} else if !ok {
		r.reason = "deny.token_bound_elsewhere"
		return
	}

	// 5. liveness, including continuous host-side attestation
	session := labels[policy.LabelSession]
	stopped, err := s.D.Stopped(ctx, session)
	if err != nil {
		r.reason = "deny.internal"
		return
	}
	if pod.Status.Phase != corev1.PodRunning || pod.GetDeletionTimestamp() != nil || stopped || !attested(pod, s.now()) {
		r.reason = "deny.session_inactive"
		return
	}

	// 6. generation fence
	gen, err := strconv.Atoi(labels[policy.LabelGeneration])
	if err != nil {
		r.reason = "deny.generation_stale"
		return
	}
	active, known, err := s.D.ActiveGen(ctx, session)
	if err != nil {
		r.reason = "deny.internal"
		return
	}
	if !known || gen != active {
		r.reason = "deny.generation_stale"
		return
	}

	// 7. release
	release := labels[policy.LabelRelease]
	digest := pod.GetAnnotations()[policy.AnnotationDigest]
	if ok, err := s.D.ReleaseOK(pod.GetNamespace(), release, digest); err != nil || !ok {
		r.reason = "deny.release_unapproved"
		return
	}
	bundle, err := s.D.Bundle(pod.GetNamespace(), release, digest)
	if err != nil || bundle == nil {
		r.reason = "deny.internal"
		return
	}
	r.bundle = bundle

	// 8 to 11. route, operation, budget, credential
	switch route {
	case "inference":
		if _, ok := bundle.Models[backend]; !ok {
			r.reason = "deny.route_not_permitted"
			return
		}
		reserved, err := s.D.Reserve(ctx, session, claims.AgentID, backend, decision, bundle)
		if errors.Is(err, policy.ErrBudget) {
			r.reason = "deny.budget_exhausted"
			return
		}
		if err != nil {
			r.reason = "deny.internal"
			return
		}
		r.reserved = reserved
		r.reason = "allow" // no upstream credential: the gateway's backend policy injects the model key
		return
	case "mcp":
		m, ok := bundle.MCP[backend]
		if !ok {
			r.reason = "deny.route_not_permitted"
			return
		}
		body := http.GetBody()
		if body == "" && len(http.GetRawBody()) > 0 {
			body = string(http.GetRawBody())
		}
		var rpc struct {
			Method string `json:"method"`
			Params struct {
				Name string `json:"name"`
			} `json:"params"`
		}
		if len(body) > 0 && json.Unmarshal([]byte(body), &rpc) != nil {
			r.reason = "deny.route_not_permitted"
			return
		}
		r.rpcMethod, r.tool = rpc.Method, rpc.Params.Name
		switch {
		case len(body) > 0 && rpc.Method == "tools/call":
			if !contains(m.Tools, rpc.Params.Name) {
				r.reason = "deny.route_not_permitted"
				return
			}
		case len(body) > 0 && (rpc.Method == "initialize" || rpc.Method == "tools/list" || rpc.Method == "ping" ||
			strings.HasPrefix(rpc.Method, "notifications/")):
		case len(body) == 0 && (http.GetMethod() == "GET" || http.GetMethod() == "DELETE"):
		default:
			r.reason = "deny.route_not_permitted"
			return
		}
		mode := m.Delegation
		if mode == "on-behalf-of" {
			has, err := s.D.HasGrant(ctx, session)
			if err != nil {
				r.reason = "deny.internal"
				return
			}
			if !has {
				if bundle.Identity.Fallback != "agent" {
					r.reason, r.delegation = "deny.delegation_required", mode
					return
				}
				mode = "agent"
			}
		}
		r.delegation = mode
		// Delegated mode authenticates as the agent client with a TokenRequest-minted
		// assertion bound to this pod and uses claims.Raw as actor_token (7.5, 9.5).
		x, err := s.D.Exchange(ctx, mode, session, backend, m.Resource, claims.Raw, pod)
		if errors.Is(err, policy.ErrDelegationRequired) {
			r.reason = "deny.delegation_required"
			return
		}
		if err != nil || x == nil || x.Token == "" {
			r.reason = "deny.exchange_failed"
			return
		}
		r.exchange = x
		r.upstream = "Bearer " + x.Token
		if mode == "on-behalf-of" {
			r.principal, _ = s.D.Principal(ctx, session)
		}
		r.reason = "allow"
		return
	default:
		r.reason = "deny.route_not_permitted"
		return
	}
}

func (r result) toProto(decision string) *authv3.CheckResponse {
	meta := map[string]any{"decision_id": decision, "reason": r.reason}
	set := map[string]string{"x-agent-decision-id": decision}
	if r.claims != nil {
		meta["agent_id"], meta["tenant"], meta["spiffe_id"] = r.claims.AgentID, r.claims.Tenant, r.claims.SpiffeID
		set["x-agent-id"], set["x-agent-tenant"], set["x-agent-spiffe-id"] = r.claims.AgentID, r.claims.Tenant, r.claims.SpiffeID
	}
	if r.pod != nil {
		l := r.pod.GetLabels()
		meta["session_id"], meta["generation"], meta["release"] = l[policy.LabelSession], l[policy.LabelGeneration], l[policy.LabelRelease]
		meta["policy_digest"] = r.pod.GetAnnotations()[policy.AnnotationDigest]
		set["x-agent-session-id"], set["x-agent-generation"], set["x-agent-release"] = l[policy.LabelSession], l[policy.LabelGeneration], l[policy.LabelRelease]
		set["x-agent-policy"] = r.pod.GetAnnotations()[policy.AnnotationDigest]
	}
	if r.bundle != nil {
		meta["tokens_per_session"] = r.bundle.Budgets.TokensPerSession
	}
	if r.reserved > 0 {
		meta["reserved_tokens"] = r.reserved
	}
	if r.principal != "" {
		meta["principal"] = r.principal
		set["x-agent-principal"] = r.principal
	}
	dyn, _ := structpb.NewStruct(meta)

	if r.reason != "allow" {
		body, _ := json.Marshal(map[string]string{"error": "forbidden", "reason": r.reason, "decision_id": decision})
		code, hdrs := typev3.StatusCode_Forbidden, []*corev3.HeaderValueOption{
			hdr("x-agent-decision-reason", r.reason), hdr("content-type", "application/json")}
		if r.reason == "deny.budget_exhausted" {
			code = typev3.StatusCode_TooManyRequests
			hdrs = append(hdrs, hdr("retry-after", "60"))
		}
		return &authv3.CheckResponse{
			Status: &status.Status{Code: int32(codes.PermissionDenied), Message: r.reason},
			HttpResponse: &authv3.CheckResponse_DeniedResponse{DeniedResponse: &authv3.DeniedHttpResponse{
				Status: &typev3.HttpStatus{Code: code}, Headers: hdrs, Body: string(body),
			}},
			DynamicMetadata: dyn,
		}
	}
	var hdrs []*corev3.HeaderValueOption
	var remove []string
	for _, name := range AgentHeaders {
		if v, ok := set[name]; ok {
			hdrs = append(hdrs, hdr(name, v))
		} else {
			remove = append(remove, name) // client-supplied values never survive
		}
	}
	if r.upstream != "" {
		hdrs = append(hdrs, hdr("authorization", r.upstream)) // MCP: the exchanged token
	} else {
		remove = append(remove, "authorization") // inference: the backend policy adds the model key
	}
	return &authv3.CheckResponse{
		Status: &status.Status{Code: int32(codes.OK)},
		HttpResponse: &authv3.CheckResponse_OkResponse{OkResponse: &authv3.OkHttpResponse{
			Headers: hdrs, HeadersToRemove: remove,
		}},
		DynamicMetadata: dyn,
	}
}

func hdr(k, v string) *corev3.HeaderValueOption {
	return &corev3.HeaderValueOption{
		Header:       &corev3.HeaderValue{Key: k, Value: v},
		AppendAction: corev3.HeaderValueOption_OVERWRITE_IF_EXISTS_OR_ADD,
	}
}

func contains(xs []string, x string) bool {
	for _, v := range xs {
		if v == x {
			return true
		}
	}
	return false
}
