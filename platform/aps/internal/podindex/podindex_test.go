package podindex

import (
	"context"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/fake"
	"k8s.io/client-go/tools/cache"

	"github.com/X-McKay/zetra/platform/aps/internal/policy"
)

func pod(name, ip string, sandbox bool) *corev1.Pod {
	l := map[string]string{}
	if sandbox {
		l[policy.LabelSandbox] = "true"
	}
	return &corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "tenant-a", Labels: l},
		Status: corev1.PodStatus{PodIP: ip}}
}

func TestPodByIP(t *testing.T) {
	cs := fake.NewSimpleClientset(pod("a", "10.0.0.1", true), pod("b", "10.0.0.2", false),
		pod("c", "10.0.0.3", true), pod("d", "10.0.0.3", true))
	idx := New(cs)
	stop := make(chan struct{})
	defer close(stop)
	idx.Run(stop)
	if !cache.WaitForCacheSync(stop, idx.HasSynced) {
		t.Fatal("no sync")
	}
	if p, n := idx.PodByIP("10.0.0.1"); n != 1 || p.Name != "a" {
		t.Fatalf("a: %v %d", p, n)
	}
	if _, n := idx.PodByIP("10.0.0.2"); n != 0 {
		t.Fatal("non-sandbox pod indexed")
	}
	if _, n := idx.PodByIP("10.0.0.3"); n != 2 {
		t.Fatal("ambiguous IP must report 2")
	}
	_ = cs.CoreV1().Pods("tenant-a").Delete(context.Background(), "a", metav1.DeleteOptions{})
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if _, n := idx.PodByIP("10.0.0.1"); n == 0 {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("deleted pod still indexed")
}
