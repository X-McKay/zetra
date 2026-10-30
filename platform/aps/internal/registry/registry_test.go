package registry

import (
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"

	"github.com/X-McKay/zetra/platform/aps/internal/policy"
)

type maps struct {
	agents, releases map[string]*unstructured.Unstructured
	cms              map[string]*corev1.ConfigMap
}

func (m *maps) Agent(ns, n string) (*unstructured.Unstructured, bool) {
	v, ok := m.agents[ns+"/"+n]
	return v, ok
}
func (m *maps) Release(ns, n string) (*unstructured.Unstructured, bool) {
	v, ok := m.releases[ns+"/"+n]
	return v, ok
}
func (m *maps) ConfigMap(ns, n string) (*corev1.ConfigMap, bool) {
	v, ok := m.cms[ns+"/"+n]
	return v, ok
}

const bundleJSON = `{"version":1,"tenant":"tenant-a","agent":"investigator","release":"rel-1",
 "identity":{"mode":"on-behalf-of","required":true,"fallback":"deny"},
 "models":{"llm-main":["m"]},"mcp":{"kubernetes":{"effect":"remote_read","tools":["pods_get"],"delegation":"on-behalf-of","resource":"https://mcp.example.com/kubernetes/mcp"}},
 "budgets":{"tokens_per_day":5000000,"tokens_per_session":200000}}`

func fixture(t *testing.T) (*Registry, *maps, string) {
	digest, err := policy.Digest([]byte(bundleJSON))
	if err != nil {
		t.Fatal(err)
	}
	m := &maps{
		agents: map[string]*unstructured.Unstructured{"tenant-a/investigator": {Object: map[string]any{
			"spec": map[string]any{"enabled": true}}}},
		releases: map[string]*unstructured.Unstructured{"tenant-a/rel-1": {Object: map[string]any{
			"spec":   map[string]any{"agent": "investigator"},
			"status": map[string]any{"ready": true, "policyDigest": digest}}}},
		cms: map[string]*corev1.ConfigMap{"tenant-a/policy-rel-1": {ObjectMeta: metav1.ObjectMeta{Name: "policy-rel-1"},
			Data: map[string]string{"bundle.json": bundleJSON}}},
	}
	return New(m), m, digest
}

func TestReleaseOK(t *testing.T) {
	r, m, d := fixture(t)
	if ok, _ := r.ReleaseOK("tenant-a", "rel-1", d); !ok {
		t.Fatal("ready release refused")
	}
	if ok, _ := r.ReleaseOK("tenant-a", "rel-1", "sha256:other"); ok {
		t.Fatal("digest mismatch accepted")
	}
	m.agents["tenant-a/investigator"].Object["spec"] = map[string]any{"enabled": false}
	if ok, _ := r.ReleaseOK("tenant-a", "rel-1", d); ok {
		t.Fatal("disabled agent accepted")
	}
	m.agents["tenant-a/investigator"].Object["spec"] = map[string]any{}
	if ok, _ := r.ReleaseOK("tenant-a", "rel-1", d); !ok {
		t.Fatal("enabled defaults to true")
	}
	m.releases["tenant-a/rel-1"].Object["status"].(map[string]any)["ready"] = false
	if ok, _ := r.ReleaseOK("tenant-a", "rel-1", d); ok {
		t.Fatal("unready release accepted")
	}
}

func TestBundleVerifiedAndCachedByDigest(t *testing.T) {
	r, m, d := fixture(t)
	b, err := r.Bundle("tenant-a", "rel-1", d)
	if err != nil || b.MCP["kubernetes"].Delegation != "on-behalf-of" || b.Budgets.TokensPerSession != 200000 || b.Identity.Fallback != "deny" {
		t.Fatalf("bundle %+v %v", b, err)
	}
	// Drifted ConfigMap content does not replace the approved, cached bundle.
	m.cms["tenant-a/policy-rel-1"].Data["bundle.json"] = `{"tenant":"tenant-a","release":"rel-1","mcp":{}}`
	if b2, err := r.Bundle("tenant-a", "rel-1", d); err != nil || b2 != b {
		t.Fatal("cache by digest")
	}
	// A bundle whose content does not match the digest is refused.
	r2, m2, d2 := fixture(t)
	m2.cms["tenant-a/policy-rel-1"].Data["bundle.json"] = `{"tenant":"tenant-a","release":"rel-1"}`
	if _, err := r2.Bundle("tenant-a", "rel-1", d2); err == nil {
		t.Fatal("tampered bundle accepted")
	}
	if _, err := r.Bundle("tenant-b", "rel-1", d); err == nil {
		t.Fatal("bundle served for another tenant")
	}
}
