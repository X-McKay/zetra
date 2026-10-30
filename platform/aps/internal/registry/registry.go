// Package registry watches Agents, AgentReleases and policy ConfigMaps and
// serves release readiness and policy bundles cached by digest (9.3 steps 7-8).
package registry

import (
	"errors"
	"strings"
	"sync"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/client-go/dynamic"
	"k8s.io/client-go/dynamic/dynamicinformer"
	"k8s.io/client-go/informers"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/tools/cache"

	"github.com/X-McKay/zetra/platform/aps/internal/policy"
)

var (
	AgentGVR   = schema.GroupVersionResource{Group: "agentplatform.io", Version: "v1alpha1", Resource: "agents"}
	ReleaseGVR = schema.GroupVersionResource{Group: "agentplatform.io", Version: "v1alpha1", Resource: "agentreleases"}
)

// Lister is what the registry reads; informers satisfy it in production and
// maps satisfy it in tests.
type Lister interface {
	Agent(ns, name string) (*unstructured.Unstructured, bool)
	Release(ns, name string) (*unstructured.Unstructured, bool)
	ConfigMap(ns, name string) (*corev1.ConfigMap, bool)
}

type Registry struct {
	L Lister

	mu      sync.Mutex
	bundles map[string]*policy.Bundle // by digest; bundles are immutable
}

func New(l Lister) *Registry { return &Registry{L: l, bundles: map[string]*policy.Bundle{}} }

// ReleaseOK is true when the release is ready, its policy digest equals the
// pod's annotation, and its Agent exists and is enabled.
func (r *Registry) ReleaseOK(ns, name, digest string) (bool, error) {
	if name == "" || digest == "" {
		return false, nil
	}
	rel, ok := r.L.Release(ns, name)
	if !ok {
		return false, nil
	}
	ready, _, _ := unstructured.NestedBool(rel.Object, "status", "ready")
	have, _, _ := unstructured.NestedString(rel.Object, "status", "policyDigest")
	agentName, _, _ := unstructured.NestedString(rel.Object, "spec", "agent")
	if !ready || have != digest || agentName == "" {
		return false, nil
	}
	ag, ok := r.L.Agent(ns, agentName)
	if !ok {
		return false, nil
	}
	enabled, found, _ := unstructured.NestedBool(ag.Object, "spec", "enabled")
	return !found || enabled, nil
}

// Bundle returns the policy bundle of release, verified against digest.
func (r *Registry) Bundle(ns, release, digest string) (*policy.Bundle, error) {
	r.mu.Lock()
	b, ok := r.bundles[digest]
	r.mu.Unlock()
	if !ok {
		cmName := "policy-" + release
		if rel, ok := r.L.Release(ns, release); ok {
			if n, _, _ := unstructured.NestedString(rel.Object, "status", "policyBundleConfigMap"); n != "" {
				cmName = n
			}
		}
		cm, ok := r.L.ConfigMap(ns, cmName)
		if !ok {
			return nil, errors.New("policy bundle ConfigMap not found")
		}
		raw := cm.Data["bundle.json"]
		if raw == "" {
			return nil, errors.New("policy bundle ConfigMap has no bundle.json")
		}
		var err error
		if b, err = policy.ParseBundle([]byte(raw), digest); err != nil {
			return nil, err
		}
		r.mu.Lock()
		r.bundles[digest] = b
		r.mu.Unlock()
	}
	if b.Tenant != ns || b.Release != release {
		return nil, errors.New("policy bundle does not belong to this release")
	}
	return b, nil
}

// Informers is the production Lister.
type Informers struct {
	agents, releases, cms cache.SharedIndexInformer
}

// keepPolicyOnly empties ConfigMaps that are not policy bundles so the cache
// holds no unrelated data.
func keepPolicyOnly(obj any) (any, error) {
	if cm, ok := obj.(*corev1.ConfigMap); ok {
		cm.ManagedFields = nil
		if !strings.HasPrefix(cm.Name, "policy-") {
			cm.Data, cm.BinaryData = nil, nil
		}
	}
	return obj, nil
}

func NewInformers(cs kubernetes.Interface, dyn dynamic.Interface) *Informers {
	df := dynamicinformer.NewDynamicSharedInformerFactory(dyn, 10*time.Minute)
	cf := informers.NewSharedInformerFactoryWithOptions(cs, 10*time.Minute, informers.WithTransform(keepPolicyOnly))
	return &Informers{
		agents:   df.ForResource(AgentGVR).Informer(),
		releases: df.ForResource(ReleaseGVR).Informer(),
		cms:      cf.Core().V1().ConfigMaps().Informer(),
	}
}

func (i *Informers) Run(stop <-chan struct{}) {
	go i.agents.Run(stop)
	go i.releases.Run(stop)
	go i.cms.Run(stop)
}

func (i *Informers) HasSynced() bool {
	return i.agents.HasSynced() && i.releases.HasSynced() && i.cms.HasSynced()
}

func getU(inf cache.SharedIndexInformer, ns, name string) (*unstructured.Unstructured, bool) {
	o, ok, err := inf.GetIndexer().GetByKey(ns + "/" + name)
	if err != nil || !ok {
		return nil, false
	}
	u, ok := o.(*unstructured.Unstructured)
	return u, ok
}

func (i *Informers) Agent(ns, name string) (*unstructured.Unstructured, bool) {
	return getU(i.agents, ns, name)
}

func (i *Informers) Release(ns, name string) (*unstructured.Unstructured, bool) {
	return getU(i.releases, ns, name)
}

func (i *Informers) ConfigMap(ns, name string) (*corev1.ConfigMap, bool) {
	o, ok, err := i.cms.GetIndexer().GetByKey(ns + "/" + name)
	if err != nil || !ok {
		return nil, false
	}
	cm, ok := o.(*corev1.ConfigMap)
	return cm, ok
}
