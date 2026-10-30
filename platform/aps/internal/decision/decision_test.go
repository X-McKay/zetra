package decision

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	corev3 "github.com/envoyproxy/go-control-plane/envoy/config/core/v3"
	authv3 "github.com/envoyproxy/go-control-plane/envoy/service/auth/v3"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/X-McKay/zetra/platform/aps/internal/policy"
	"github.com/X-McKay/zetra/platform/aps/internal/testutil"
)

const (
	session   = "6f1d2c3e-0000-4000-8000-000000000001"
	digest    = "sha256:9c1e000000000000000000000000000000000000000000000000000000000000"
	podIP     = "10.42.1.57"
	inbound   = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJhZ2VudCJ9.inbound-agent-token-signature"
	exchanged = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJhbGljZSJ9.exchanged-upstream-signature"
)

type fixture struct {
	d      Deps
	pod    *corev1.Pod
	claims *policy.AgentClaims
	bundle *policy.Bundle
	verify atomic.Int32
}

func newFixture() *fixture {
	now := time.Now()
	f := &fixture{
		pod: &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name: "s-3f9a1c0b7e2d4a61-g1", Namespace: "tenant-a", UID: "pod-uid-1",
				Labels: map[string]string{
					policy.LabelSandbox: "true", policy.LabelAgent: "investigator", "agentplatform.io/tenant": "tenant-a",
					policy.LabelSession: session, policy.LabelGeneration: "1", policy.LabelRelease: "investigator-2026-10-29-a93557ad",
				},
				Annotations: map[string]string{
					policy.AnnotationDigest: digest,
					policy.AnnotationAttest: now.Add(-10*time.Second).UTC().Format(time.RFC3339) + ";chain",
				},
			},
			Status: corev1.PodStatus{Phase: corev1.PodRunning, PodIP: podIP},
		},
		claims: &policy.AgentClaims{
			Sub: "9b2c", JTI: "7a0e", AgentID: "tenant-a/investigator", Tenant: "tenant-a",
			SpiffeID: "spiffe://cluster.local/ns/tenant-a/sa/agent-investigator", ClientID: "agent-tenant-a-investigator",
			ClientType: "agent", Exp: now.Add(300 * time.Second),
		},
		bundle: &policy.Bundle{
			Tenant: "tenant-a", Agent: "investigator", Release: "investigator-2026-10-29-a93557ad",
			Models: map[string][]string{"llm-main": {"Qwen3.6-35B-A3B-NVFP4"}},
			MCP: map[string]policy.MCPBackend{"kubernetes": {
				Tools: []string{"events_list", "pods_get", "pods_list_in_namespace"}, Delegation: "on-behalf-of",
				Resource: "https://mcp.example.com/kubernetes/mcp",
			}},
		},
	}
	f.bundle.Budgets.TokensPerSession = 200000
	f.bundle.Identity.Fallback = "deny"
	f.d = Deps{
		Verify: func(_ context.Context, bearer string) (*policy.AgentClaims, error) {
			f.verify.Add(1)
			if bearer != inbound {
				return nil, errors.New("bad token")
			}
			c := *f.claims
			c.Raw = bearer
			return &c, nil
		},
		PodByIP: func(ip string) (*corev1.Pod, int) {
			if ip == podIP {
				return f.pod, 1
			}
			return nil, 0
		},
		PinOK:     func(context.Context, string, string, time.Time) (bool, error) { return true, nil },
		ActiveGen: func(context.Context, string) (int, bool, error) { return 1, true, nil },
		Stopped:   func(context.Context, string) (bool, error) { return false, nil },
		ReleaseOK: func(_, _, d string) (bool, error) { return d == digest, nil },
		Bundle:    func(_, _, _ string) (*policy.Bundle, error) { return f.bundle, nil },
		Reserve: func(context.Context, string, string, string, string, *policy.Bundle) (int, error) {
			return 32768, nil
		},
		HasGrant:  func(context.Context, string) (bool, error) { return true, nil },
		Principal: func(context.Context, string) (string, error) { return "alice", nil },
		Exchange: func(_ context.Context, mode, _, _, _, actor string, _ *corev1.Pod) (*policy.Exchanged, error) {
			if actor != inbound {
				return nil, errors.New("actor token not passed")
			}
			return &policy.Exchanged{Token: exchanged, UpstreamJTI: "d41a"}, nil
		},
	}
	return f
}

type reqOpt func(*authv3.AttributeContext_HttpRequest, map[string]string)

func header(k, v string) reqOpt {
	return func(h *authv3.AttributeContext_HttpRequest, _ map[string]string) { h.Headers[k] = v }
}

func body(b string) reqOpt {
	return func(h *authv3.AttributeContext_HttpRequest, _ map[string]string) { h.Body = b }
}

func backend(b string) reqOpt {
	return func(_ *authv3.AttributeContext_HttpRequest, e map[string]string) { e["backend"] = b }
}

func toolCall(name string) string {
	return `{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"` + name + `","arguments":{}}}`
}

func checkReq(route string, opts ...reqOpt) *authv3.CheckRequest {
	h := &authv3.AttributeContext_HttpRequest{
		Method: "POST", Path: "/kubernetes/mcp",
		Headers: map[string]string{"authorization": "Bearer " + inbound, "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"},
	}
	ext := map[string]string{"route": route, "backend": "kubernetes"}
	if route == "inference" {
		h.Path, ext["backend"] = "/models/llm-main/v1/chat/completions", "llm-main"
	} else {
		h.Body = toolCall("pods_get")
	}
	for _, o := range opts {
		o(h, ext)
	}
	return &authv3.CheckRequest{Attributes: &authv3.AttributeContext{
		Source: &authv3.AttributeContext_Peer{Address: &corev3.Address{Address: &corev3.Address_SocketAddress{
			SocketAddress: &corev3.SocketAddress{Address: podIP}}}},
		Request:           &authv3.AttributeContext_Request{Http: h},
		ContextExtensions: ext,
	}}
}

func (f *fixture) check(t *testing.T, req *authv3.CheckRequest) (*authv3.CheckResponse, string) {
	t.Helper()
	s := &Server{D: f.d}
	resp, err := s.Check(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	return resp, resp.GetDynamicMetadata().GetFields()["reason"].GetStringValue()
}

func okHeaders(resp *authv3.CheckResponse) (map[string]string, []string) {
	set := map[string]string{}
	for _, h := range resp.GetOkResponse().GetHeaders() {
		set[h.GetHeader().GetKey()] = h.GetHeader().GetValue()
	}
	return set, resp.GetOkResponse().GetHeadersToRemove()
}

func TestAllowMCPAndInference(t *testing.T) {
	f := newFixture()
	if _, r := f.check(t, checkReq("mcp")); r != "allow" {
		t.Fatalf("mcp: %s", r)
	}
	if _, r := f.check(t, checkReq("inference")); r != "allow" {
		t.Fatalf("inference: %s", r)
	}
}

// Each breaker makes one step of 9.3 fail. Applying breakers i..n together must
// still yield reason i: the first failure wins.
type breaker struct {
	reason string
	apply  func(f *fixture)
}

func mcpBreakers() []breaker {
	return []breaker{
		{"deny.token_invalid", func(f *fixture) {
			f.d.Verify = func(context.Context, string) (*policy.AgentClaims, error) { return nil, errors.New("sig") }
		}},
		{"deny.token_not_agent", func(f *fixture) { f.claims.ClientType = "user" }},
		{"deny.pod_unknown", func(f *fixture) { f.d.PodByIP = func(string) (*corev1.Pod, int) { return nil, 0 } }},
		{"deny.pod_agent_mismatch", func(f *fixture) { f.pod.Labels[policy.LabelAgent] = "other" }},
		{"deny.token_bound_elsewhere", func(f *fixture) {
			f.d.PinOK = func(context.Context, string, string, time.Time) (bool, error) { return false, nil }
		}},
		{"deny.session_inactive", func(f *fixture) {
			f.d.Stopped = func(context.Context, string) (bool, error) { return true, nil }
		}},
		{"deny.generation_stale", func(f *fixture) {
			f.d.ActiveGen = func(context.Context, string) (int, bool, error) { return 2, true, nil }
		}},
		{"deny.release_unapproved", func(f *fixture) {
			f.d.ReleaseOK = func(_, _, _ string) (bool, error) { return false, nil }
		}},
		{"deny.route_not_permitted", func(f *fixture) {
			f.bundle.MCP["kubernetes"] = policy.MCPBackend{Tools: []string{"events_list"}, Delegation: "on-behalf-of"}
		}},
		{"deny.delegation_required", func(f *fixture) {
			f.d.HasGrant = func(context.Context, string) (bool, error) { return false, nil }
		}},
		{"deny.exchange_failed", func(f *fixture) {
			f.d.Exchange = func(context.Context, string, string, string, string, string, *corev1.Pod) (*policy.Exchanged, error) {
				return nil, errors.New("idp 4xx")
			}
		}},
	}
}

func TestEveryDenyReasonReachableInOrder(t *testing.T) {
	bs := mcpBreakers()
	for i := range bs {
		f := newFixture()
		for _, b := range bs[i:] {
			b.apply(f)
		}
		resp, r := f.check(t, checkReq("mcp"))
		if r != bs[i].reason {
			t.Errorf("breakers %d..: got %s, want %s", i, r, bs[i].reason)
		}
		if resp.GetDeniedResponse().GetStatus().GetCode() != 403 {
			t.Errorf("%s: status %d", r, resp.GetDeniedResponse().GetStatus().GetCode())
		}
	}
	// Inference: route, then budget.
	f := newFixture()
	f.d.Reserve = func(context.Context, string, string, string, string, *policy.Bundle) (int, error) {
		return 0, policy.ErrBudget
	}
	if _, r := f.check(t, checkReq("inference")); r != "deny.budget_exhausted" {
		t.Fatalf("budget: %s", r)
	}
	if _, r := f.check(t, checkReq("inference", backend("llm-other"))); r != "deny.route_not_permitted" {
		t.Fatalf("unknown model backend before budget: %s", r)
	}
	if _, r := f.check(t, checkReq("other")); r != "deny.route_not_permitted" {
		t.Fatalf("unknown route: %s", r)
	}
}

func TestMissingBearerAndAgentClaims(t *testing.T) {
	f := newFixture()
	if _, r := f.check(t, checkReq("mcp", header("authorization", ""))); r != "deny.token_invalid" {
		t.Fatal(r)
	}
	if _, r := f.check(t, checkReq("mcp", header("authorization", "Basic "+inbound))); r != "deny.token_invalid" {
		t.Fatal(r)
	}
	for _, mut := range []func(c *policy.AgentClaims){
		func(c *policy.AgentClaims) { c.AgentID = "" },
		func(c *policy.AgentClaims) { c.Tenant = "" },
		func(c *policy.AgentClaims) { c.JTI = "" },
	} {
		f := newFixture()
		mut(f.claims)
		if _, r := f.check(t, checkReq("mcp")); r != "deny.token_not_agent" {
			t.Fatal(r)
		}
	}
}

func TestPodUnknownWhenAmbiguous(t *testing.T) {
	f := newFixture()
	f.d.PodByIP = func(string) (*corev1.Pod, int) { return nil, 2 }
	if _, r := f.check(t, checkReq("mcp")); r != "deny.pod_unknown" {
		t.Fatal(r)
	}
}

func TestT26TokenBoundElsewhereAndAgentMismatch(t *testing.T) {
	// Copied to another pod of the same agent: the pin names a different pod.
	f := newFixture()
	f.d.PinOK = func(_ context.Context, jti, uid string, _ time.Time) (bool, error) {
		return uid == "pod-uid-other", nil
	}
	if _, r := f.check(t, checkReq("mcp")); r != "deny.token_bound_elsewhere" {
		t.Fatal(r)
	}
	// Copied to agent B's pod.
	f = newFixture()
	f.pod.Labels[policy.LabelAgent] = "agent-b"
	if _, r := f.check(t, checkReq("mcp")); r != "deny.pod_agent_mismatch" {
		t.Fatal(r)
	}
	// Same agent name in another tenant.
	f = newFixture()
	f.pod.Namespace = "tenant-b"
	if _, r := f.check(t, checkReq("mcp")); r != "deny.pod_agent_mismatch" {
		t.Fatal(r)
	}
}

func TestSessionInactiveVariants(t *testing.T) {
	cases := map[string]func(f *fixture){
		"pending":     func(f *fixture) { f.pod.Status.Phase = corev1.PodPending },
		"terminating": func(f *fixture) { ts := metav1.Now(); f.pod.DeletionTimestamp = &ts },
		"stopped (T15)": func(f *fixture) {
			f.d.Stopped = func(context.Context, string) (bool, error) { return true, nil }
		},
		"no attestation": func(f *fixture) { delete(f.pod.Annotations, policy.AnnotationAttest) },
		"stale attestation": func(f *fixture) {
			f.pod.Annotations[policy.AnnotationAttest] = time.Now().Add(-91*time.Second).UTC().Format(time.RFC3339) + ";c"
		},
	}
	for name, mut := range cases {
		f := newFixture()
		mut(f)
		if _, r := f.check(t, checkReq("mcp")); r != "deny.session_inactive" {
			t.Errorf("%s: %s", name, r)
		}
	}
}

func TestT21GenerationStale(t *testing.T) {
	f := newFixture()
	f.d.ActiveGen = func(context.Context, string) (int, bool, error) { return 2, true, nil }
	if _, r := f.check(t, checkReq("mcp")); r != "deny.generation_stale" {
		t.Fatal(r)
	}
	f = newFixture()
	f.d.ActiveGen = func(context.Context, string) (int, bool, error) { return 0, false, nil }
	if _, r := f.check(t, checkReq("mcp")); r != "deny.generation_stale" {
		t.Fatalf("no recorded generation: %s", r)
	}
}

func TestT15ReleaseUnapprovedOrAgentDisabled(t *testing.T) {
	f := newFixture()
	f.d.ReleaseOK = func(_, _, _ string) (bool, error) { return false, nil } // disabled or unapproved
	if _, r := f.check(t, checkReq("inference")); r != "deny.release_unapproved" {
		t.Fatal(r)
	}
	f = newFixture()
	f.pod.Annotations[policy.AnnotationDigest] = "sha256:drifted"
	if _, r := f.check(t, checkReq("inference")); r != "deny.release_unapproved" {
		t.Fatalf("digest mismatch: %s", r)
	}
}

func TestMCPMethods(t *testing.T) {
	cases := map[string]struct {
		method, body string
		want         string
	}{
		"initialize":    {"POST", `{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}`, "allow"},
		"tools/list":    {"POST", `{"jsonrpc":"2.0","id":1,"method":"tools/list"}`, "allow"},
		"ping":          {"POST", `{"jsonrpc":"2.0","id":1,"method":"ping"}`, "allow"},
		"notification":  {"POST", `{"jsonrpc":"2.0","method":"notifications/initialized"}`, "allow"},
		"GET stream":    {"GET", "", "allow"},
		"DELETE":        {"DELETE", "", "allow"},
		"empty POST":    {"POST", "", "deny.route_not_permitted"},
		"resources":     {"POST", `{"jsonrpc":"2.0","id":1,"method":"resources/read"}`, "deny.route_not_permitted"},
		"unparsable":    {"POST", `{"method":`, "deny.route_not_permitted"},
		"tool not held": {"POST", toolCall("pods_delete"), "deny.route_not_permitted"},
	}
	for name, c := range cases {
		f := newFixture()
		req := checkReq("mcp", body(c.body))
		req.Attributes.Request.Http.Method = c.method
		if _, r := f.check(t, req); r != c.want {
			t.Errorf("%s: %s, want %s", name, r, c.want)
		}
	}
	f := newFixture()
	if _, r := f.check(t, checkReq("mcp", backend("github"))); r != "deny.route_not_permitted" {
		t.Errorf("backend not in bundle: %s", r)
	}
}

func TestDelegationFallbackToAgent(t *testing.T) {
	f := newFixture()
	f.bundle.Identity.Fallback = "agent"
	f.d.HasGrant = func(context.Context, string) (bool, error) { return false, nil }
	var gotMode string
	f.d.Exchange = func(_ context.Context, mode, _, _, _, _ string, _ *corev1.Pod) (*policy.Exchanged, error) {
		gotMode = mode
		return &policy.Exchanged{Token: exchanged}, nil
	}
	resp, r := f.check(t, checkReq("mcp"))
	if r != "allow" || gotMode != "agent" {
		t.Fatalf("%s %s", r, gotMode)
	}
	set, remove := okHeaders(resp)
	if _, ok := set["x-agent-principal"]; ok || !contains(remove, "x-agent-principal") {
		t.Fatal("principal must be removed in agent mode")
	}
}

func TestDelegationRequiredFromExchange(t *testing.T) {
	f := newFixture()
	f.d.Exchange = func(context.Context, string, string, string, string, string, *corev1.Pod) (*policy.Exchanged, error) {
		return nil, policy.ErrDelegationRequired
	}
	if _, r := f.check(t, checkReq("mcp")); r != "deny.delegation_required" {
		t.Fatal(r)
	}
}

// T8: forged x-agent-* headers are removed or overwritten, and the decision
// reason never reaches the upstream.
func TestT8ForgedAgentHeadersRemoved(t *testing.T) {
	f := newFixture()
	req := checkReq("mcp",
		header("x-agent-id", "tenant-test/other"), header("x-agent-principal", "mallory"),
		header("x-agent-session-id", "0000"), header("x-agent-decision-reason", "allow"))
	resp, r := f.check(t, req)
	if r != "allow" {
		t.Fatal(r)
	}
	set, remove := okHeaders(resp)
	for _, h := range resp.GetOkResponse().GetHeaders() {
		if h.GetAppendAction() != corev3.HeaderValueOption_OVERWRITE_IF_EXISTS_OR_ADD {
			t.Errorf("%s not overwrite", h.GetHeader().GetKey())
		}
	}
	for _, name := range AgentHeaders {
		_, isSet := set[name]
		if isSet == contains(remove, name) {
			t.Errorf("%s: set=%v removed=%v; exactly one must hold", name, isSet, contains(remove, name))
		}
	}
	if set["x-agent-id"] != "tenant-a/investigator" || set["x-agent-session-id"] != session || set["x-agent-principal"] != "alice" {
		t.Fatalf("APS values not set: %v", set)
	}
	if _, ok := set["x-agent-decision-reason"]; ok || !contains(remove, "x-agent-decision-reason") {
		t.Fatal("x-agent-decision-reason must be absent from the ok response and removed")
	}
	all, _ := json.Marshal(resp.GetOkResponse())
	for _, forged := range []string{"tenant-test/other", "mallory", `"0000"`} {
		if strings.Contains(string(all), forged) {
			t.Errorf("forged value %s survives", forged)
		}
	}
	// Inference: the same hygiene, and principal removed.
	resp, _ = f.check(t, checkReq("inference", header("x-agent-principal", "mallory")))
	set, remove = okHeaders(resp)
	if _, ok := set["x-agent-principal"]; ok || !contains(remove, "x-agent-principal") || !contains(remove, "x-agent-decision-reason") {
		t.Fatal("inference hygiene")
	}
}

// T7/T12 logic: inference removes the inbound Authorization; MCP replaces it
// with the exchanged token and never forwards the inbound one.
func TestT7T12UpstreamAuthorization(t *testing.T) {
	f := newFixture()
	resp, _ := f.check(t, checkReq("inference"))
	set, remove := okHeaders(resp)
	if _, ok := set["authorization"]; ok || !contains(remove, "authorization") {
		t.Fatal("inference must remove authorization and set none")
	}
	resp, _ = f.check(t, checkReq("mcp"))
	set, remove = okHeaders(resp)
	if set["authorization"] != "Bearer "+exchanged {
		t.Fatalf("mcp authorization = %q", set["authorization"])
	}
	if contains(remove, "authorization") {
		t.Fatal("mcp must overwrite, not remove, authorization")
	}
	all, _ := json.Marshal(resp)
	if strings.Contains(string(all), inbound) {
		t.Fatal("inbound token appears in the response")
	}
}

func TestDeniedResponseShape(t *testing.T) {
	f := newFixture()
	f.d.Reserve = func(context.Context, string, string, string, string, *policy.Bundle) (int, error) {
		return 0, policy.ErrBudget
	}
	resp, _ := f.check(t, checkReq("inference"))
	d := resp.GetDeniedResponse()
	if d.GetStatus().GetCode() != 429 {
		t.Fatalf("budget status %d", d.GetStatus().GetCode())
	}
	hs := map[string]string{}
	for _, h := range d.GetHeaders() {
		hs[h.GetHeader().GetKey()] = h.GetHeader().GetValue()
	}
	if hs["retry-after"] != "60" || hs["x-agent-decision-reason"] != "deny.budget_exhausted" {
		t.Fatalf("headers %v", hs)
	}
	var b map[string]string
	if err := json.Unmarshal([]byte(d.GetBody()), &b); err != nil {
		t.Fatal(err)
	}
	id := resp.GetDynamicMetadata().GetFields()["decision_id"].GetStringValue()
	if b["error"] != "forbidden" || b["reason"] != "deny.budget_exhausted" || b["decision_id"] != id || id == "" {
		t.Fatalf("body %v", b)
	}
	f = newFixture()
	f.d.Stopped = func(context.Context, string) (bool, error) { return true, nil }
	resp, _ = f.check(t, checkReq("mcp"))
	if resp.GetDeniedResponse().GetStatus().GetCode() != 403 {
		t.Fatal("403 expected")
	}
	for _, h := range resp.GetDeniedResponse().GetHeaders() {
		if h.GetHeader().GetKey() == "retry-after" {
			t.Fatal("retry-after on a non-budget denial")
		}
	}
}

func TestDynamicMetadata(t *testing.T) {
	f := newFixture()
	resp, _ := f.check(t, checkReq("inference"))
	m := resp.GetDynamicMetadata().GetFields()
	for _, k := range []string{"agent_id", "spiffe_id", "tenant", "session_id", "generation", "release", "policy_digest", "decision_id", "reason", "tokens_per_session", "reserved_tokens"} {
		if _, ok := m[k]; !ok {
			t.Errorf("metadata %s missing", k)
		}
	}
	if m["reserved_tokens"].GetNumberValue() != 32768 || m["tokens_per_session"].GetNumberValue() != 200000 {
		t.Fatal("metadata values")
	}
	resp, _ = f.check(t, checkReq("mcp"))
	if resp.GetDynamicMetadata().GetFields()["principal"].GetStringValue() != "alice" {
		t.Fatal("principal metadata")
	}
}

// T17 logic: any dependency error, panic or timeout denies; nothing fails open.
func TestT17DependencyErrorsFailClosed(t *testing.T) {
	boom := errors.New("redis down")
	cases := map[string]func(f *fixture){
		"pin": func(f *fixture) {
			f.d.PinOK = func(context.Context, string, string, time.Time) (bool, error) { return true, boom }
		},
		"stopped": func(f *fixture) { f.d.Stopped = func(context.Context, string) (bool, error) { return false, boom } },
		"generation": func(f *fixture) {
			f.d.ActiveGen = func(context.Context, string) (int, bool, error) { return 1, true, boom }
		},
		"release": func(f *fixture) { f.d.ReleaseOK = func(_, _, _ string) (bool, error) { return true, boom } },
		"bundle":  func(f *fixture) { f.d.Bundle = func(_, _, _ string) (*policy.Bundle, error) { return nil, boom } },
		"grant":   func(f *fixture) { f.d.HasGrant = func(context.Context, string) (bool, error) { return true, boom } },
		"exchange": func(f *fixture) {
			f.d.Exchange = func(context.Context, string, string, string, string, string, *corev1.Pod) (*policy.Exchanged, error) {
				return &policy.Exchanged{Token: exchanged}, boom
			}
		},
		"panic":   func(f *fixture) { f.d.PodByIP = func(string) (*corev1.Pod, int) { panic("informer") } },
		"nil dep": func(f *fixture) { f.d.Stopped = nil },
		"timeout": func(f *fixture) {
			f.d.Stopped = func(ctx context.Context, _ string) (bool, error) { <-ctx.Done(); return false, ctx.Err() }
		},
		"slow allow": func(f *fixture) {
			f.d.Principal = func(ctx context.Context, _ string) (string, error) { <-ctx.Done(); return "alice", nil }
		},
	}
	for name, mut := range cases {
		f := newFixture()
		mut(f)
		resp, r := f.check(t, checkReq("mcp"))
		if r == "allow" || resp.GetOkResponse() != nil {
			t.Errorf("%s: allowed", name)
		}
	}
	f := newFixture()
	f.d.Reserve = func(context.Context, string, string, string, string, *policy.Bundle) (int, error) { return 0, boom }
	if _, r := f.check(t, checkReq("inference")); r != "deny.internal" {
		t.Fatalf("reserve error: %s", r)
	}
	f = newFixture()
	f.d.Verify = func(context.Context, string) (*policy.AgentClaims, error) { panic("x") }
	if _, r := f.check(t, checkReq("inference")); r != "deny.internal" {
		t.Fatalf("panic: %s", r)
	}
}

// T19 logic: no decision is cached; alternating an allowed and a forbidden
// tool with one token yields an exact alternation, one evaluation per request.
func TestT19NoDecisionCaching(t *testing.T) {
	f := newFixture()
	var buf bytes.Buffer
	s := &Server{D: f.d, Log: NewLogger(&buf)}
	for i := 0; i < 50; i++ {
		for _, c := range []struct{ tool, want string }{{"pods_get", "allow"}, {"pods_delete", "deny.route_not_permitted"}} {
			resp, err := s.Check(context.Background(), checkReq("mcp", body(toolCall(c.tool))))
			if err != nil {
				t.Fatal(err)
			}
			if r := resp.GetDynamicMetadata().GetFields()["reason"].GetStringValue(); r != c.want {
				t.Fatalf("iteration %d tool %s: %s", i, c.tool, r)
			}
		}
		for _, c := range []struct{ be, want string }{{"llm-main", "allow"}, {"llm-other", "deny.route_not_permitted"}} {
			resp, _ := s.Check(context.Background(), checkReq("inference", backend(c.be)))
			if r := resp.GetDynamicMetadata().GetFields()["reason"].GetStringValue(); r != c.want {
				t.Fatalf("iteration %d model %s: %s", i, c.be, r)
			}
		}
	}
	if n := f.verify.Load(); n != 200 {
		t.Fatalf("token verified %d times, want 200", n)
	}
	lines := strings.Split(strings.TrimSpace(buf.String()), "\n")
	ids := map[string]bool{}
	for _, l := range lines {
		var e map[string]any
		if err := json.Unmarshal([]byte(l), &e); err != nil {
			t.Fatal(err)
		}
		ids[e["decision_id"].(string)] = true
	}
	if len(lines) != 200 || len(ids) != 200 {
		t.Fatalf("log lines %d, distinct decisions %d", len(lines), len(ids))
	}
}

func TestDecisionLogFieldsAndRedaction(t *testing.T) {
	idp := testutil.NewIdP(t)
	realInbound := idp.Sign(t, testutil.AgentClaims("7a0e"))
	f := newFixture()
	verify := f.d.Verify
	f.d.Verify = func(ctx context.Context, b string) (*policy.AgentClaims, error) {
		if b == realInbound {
			b = inbound
		}
		return verify(ctx, b)
	}
	var buf bytes.Buffer
	s := &Server{D: f.d, Log: NewLogger(&buf)}
	reqs := []*authv3.CheckRequest{
		checkReq("mcp", header("authorization", "Bearer "+realInbound)),
		checkReq("inference", header("authorization", "Bearer "+realInbound)),
		checkReq("mcp", header("authorization", "Bearer "+realInbound), body(toolCall("pods_delete"))),
		checkReq("mcp", header("authorization", "Bearer garbage-token-value-that-is-long")),
	}
	reqs[0].Attributes.Request.Http.Path = "/kubernetes/mcp?access_token=" + realInbound
	for _, r := range reqs {
		if _, err := s.Check(context.Background(), r); err != nil {
			t.Fatal(err)
		}
	}
	out := buf.String()
	testutil.AssertRedacted(t, out)
	for _, secret := range []string{inbound, exchanged, realInbound, "garbage-token-value", "pods_get\",\"arguments"} {
		if strings.Contains(out, secret) {
			t.Fatalf("log contains %q", secret)
		}
	}
	var e map[string]any
	_ = json.Unmarshal([]byte(strings.Split(out, "\n")[0]), &e)
	want := map[string]any{
		"reason": "allow", "tenant": "tenant-a", "agent_id": "tenant-a/investigator", "session_id": session,
		"generation": float64(1), "route": "mcp", "backend": "kubernetes", "path": "/kubernetes/mcp",
		"method": "tools/call", "tool": "pods_get", "delegation": "on-behalf-of", "principal": "alice",
		"source_ip": podIP, "pod": "s-3f9a1c0b7e2d4a61-g1", "token_jti": "7a0e", "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
		"policy_digest": digest, "model": nil,
	}
	for k, v := range want {
		if e[k] != v {
			t.Errorf("log %s = %v, want %v", k, e[k], v)
		}
	}
	if x, ok := e["exchange"].(map[string]any); !ok || x["upstream_jti"] != "d41a" {
		t.Errorf("exchange field %v", e["exchange"])
	}
	// A denial before the pod step leaves later fields null.
	var d map[string]any
	_ = json.Unmarshal([]byte(strings.Split(strings.TrimSpace(out), "\n")[3]), &d)
	if d["reason"] != "deny.token_invalid" || d["session_id"] != nil || d["tenant"] != nil {
		t.Errorf("denied log %v", d)
	}
}
