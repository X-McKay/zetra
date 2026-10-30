// Package podindex indexes sandbox pods (label agentplatform.io/sandbox=true)
// by pod IP from a client-go informer (9.4).
package podindex

import (
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/informers"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/tools/cache"

	"github.com/X-McKay/zetra/platform/aps/internal/policy"
)

const byIP = "ip"

type Index struct {
	inf cache.SharedIndexInformer
}

func ipIndex(obj any) ([]string, error) {
	p, ok := obj.(*corev1.Pod)
	if !ok {
		return nil, nil
	}
	seen := map[string]bool{}
	var out []string
	add := func(ip string) {
		if ip != "" && !seen[ip] {
			seen[ip] = true
			out = append(out, ip)
		}
	}
	add(p.Status.PodIP)
	for _, ip := range p.Status.PodIPs {
		add(ip.IP)
	}
	return out, nil
}

// trim drops fields the APS never reads, to bound informer memory.
func trim(obj any) (any, error) {
	if p, ok := obj.(*corev1.Pod); ok {
		p.ManagedFields = nil
		p.Spec = corev1.PodSpec{}
	}
	return obj, nil
}

// New builds the informer; call Run to start it.
func New(cs kubernetes.Interface) *Index {
	f := informers.NewSharedInformerFactoryWithOptions(cs, 10*time.Minute,
		informers.WithTweakListOptions(func(o *metav1.ListOptions) { o.LabelSelector = policy.LabelSandbox + "=true" }),
		informers.WithTransform(trim))
	inf := f.Core().V1().Pods().Informer()
	_ = inf.AddIndexers(cache.Indexers{byIP: ipIndex})
	return &Index{inf: inf}
}

// FromInformer wraps an existing informer (tests).
func FromInformer(inf cache.SharedIndexInformer) *Index {
	_ = inf.AddIndexers(cache.Indexers{byIP: ipIndex})
	return &Index{inf: inf}
}

func (i *Index) Run(stop <-chan struct{}) { go i.inf.Run(stop) }
func (i *Index) HasSynced() bool          { return i.inf.HasSynced() }

// PodByIP returns the sandbox pod with this IP and how many matched.
func (i *Index) PodByIP(ip string) (*corev1.Pod, int) {
	if ip == "" {
		return nil, 0
	}
	objs, err := i.inf.GetIndexer().ByIndex(byIP, ip)
	if err != nil {
		return nil, 0
	}
	var pods []*corev1.Pod
	for _, o := range objs {
		if p, ok := o.(*corev1.Pod); ok && p.Labels[policy.LabelSandbox] == "true" {
			pods = append(pods, p)
		}
	}
	if len(pods) != 1 {
		return nil, len(pods)
	}
	return pods[0], 1
}

// Pod returns the sandbox pod by namespace and name.
func (i *Index) Pod(ns, name string) (*corev1.Pod, bool) {
	o, ok, err := i.inf.GetIndexer().GetByKey(ns + "/" + name)
	if err != nil || !ok {
		return nil, false
	}
	p, ok := o.(*corev1.Pod)
	return p, ok
}
