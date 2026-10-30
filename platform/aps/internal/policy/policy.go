// Package policy holds the types shared by the decision path: the policy bundle
// of spec 6.3, the agent token claims, and the sentinel errors of 9.3.
package policy

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"regexp"
	"time"
)

var (
	// ErrBudget is returned by the ledger when a reservation does not fit (9.3 step 10).
	ErrBudget = errors.New("budget exhausted")
	// ErrDelegationRequired is returned when the vaulted subject token is missing,
	// expired or could not be refreshed (9.5).
	ErrDelegationRequired = errors.New("delegation required")
)

// TenantRe is the tenant namespace pattern of 3.1.
var TenantRe = regexp.MustCompile(`^tenant-[a-z0-9-]{1,50}$`)

// SessionRe is a lower-case UUID (3.6).
var SessionRe = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)

// Label and annotation keys (3.6, 5.5).
const (
	LabelSandbox       = "agentplatform.io/sandbox"
	LabelAgent         = "agentplatform.io/agent"
	LabelSession       = "agentplatform.io/session"
	LabelGeneration    = "agentplatform.io/generation"
	LabelRelease       = "agentplatform.io/release"
	AnnotationDigest   = "agentplatform.io/policy-digest"
	AnnotationAttest   = "agentplatform.io/netpol-attested"
	AgentGatewayAud    = "agent-gateway"
	PolicyServiceAud   = "agent-policy-service"
	TokenTypeAccess    = "urn:ietf:params:oauth:token-type:access_token"
	AssertionTypeJWT   = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
	GrantTokenExchange = "urn:ietf:params:oauth:grant-type:token-exchange"
)

// AgentClaims are the verified claims of an agent access token (4.2).
type AgentClaims struct {
	Sub, JTI, AgentID, Tenant, SpiffeID, ClientID, ClientType string
	Exp                                                       time.Time
	Raw                                                       string // never logged
}

type MCPBackend struct {
	Effect     string   `json:"effect"`
	Tools      []string `json:"tools"`
	Delegation string   `json:"delegation"`
	Resource   string   `json:"resource"`
}

// Bundle is the policy bundle of 6.3, read from ConfigMap policy-<release>.
type Bundle struct {
	Version  int    `json:"version"`
	Tenant   string `json:"tenant"`
	Agent    string `json:"agent"`
	Release  string `json:"release"`
	ClientID string `json:"client_id"`
	SpiffeID string `json:"spiffe_id"`
	Identity struct {
		Mode     string `json:"mode"`
		Fallback string `json:"fallback"`
		Required bool   `json:"required"`
	} `json:"identity"`
	Models  map[string][]string   `json:"models"`
	MCP     map[string]MCPBackend `json:"mcp"`
	Budgets struct {
		TokensPerSession int64 `json:"tokens_per_session"`
		TokensPerDay     int64 `json:"tokens_per_day"`
	} `json:"budgets"`
}

// Exchanged is the result of a token exchange (9.5), as the decision path sees it.
type Exchanged struct {
	Token       string // never logged
	UpstreamJTI string
	Cached      bool
	Duration    time.Duration
}

// Digest returns "sha256:<hex>" over the canonical JSON form of raw: keys sorted,
// no whitespace, UTF-8 without HTML or non-ASCII escaping (3.6).
func Digest(raw []byte) (string, error) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var v any
	if err := dec.Decode(&v); err != nil {
		return "", err
	}
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return "", err
	}
	sum := sha256.Sum256(bytes.TrimSuffix(buf.Bytes(), []byte("\n")))
	return "sha256:" + hex.EncodeToString(sum[:]), nil
}

// ParseBundle verifies raw against digest (canonical form, or the raw bytes as
// stored) and decodes it.
func ParseBundle(raw []byte, digest string) (*Bundle, error) {
	canon, err := Digest(raw)
	if err != nil {
		return nil, err
	}
	sum := sha256.Sum256(raw)
	if canon != digest && "sha256:"+hex.EncodeToString(sum[:]) != digest {
		return nil, errors.New("policy bundle digest mismatch")
	}
	var b Bundle
	if err := json.Unmarshal(raw, &b); err != nil {
		return nil, err
	}
	return &b, nil
}
