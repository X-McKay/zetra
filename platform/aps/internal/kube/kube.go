// Package kube wraps the three Kubernetes API calls the APS makes: TokenReview,
// SubjectAccessReview and TokenRequest.
package kube

import (
	"context"
	"errors"
	"slices"

	authnv1 "k8s.io/api/authentication/v1"
	authzv1 "k8s.io/api/authorization/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
)

// Pod extras a TokenReview returns for a pod-bound ServiceAccount token.
const (
	ExtraPodName = "authentication.kubernetes.io/pod-name"
	ExtraPodUID  = "authentication.kubernetes.io/pod-uid"
)

// Identity is an authenticated Kubernetes caller.
type Identity struct {
	Username string
	UID      string
	Groups   []string
	Extra    map[string][]string
}

func (i Identity) extra(k string) string {
	if v := i.Extra[k]; len(v) == 1 {
		return v[0]
	}
	return ""
}

func (i Identity) PodName() string { return i.extra(ExtraPodName) }
func (i Identity) PodUID() string  { return i.extra(ExtraPodUID) }

// API is the subset of the Kubernetes API the APS uses; tests fake it.
type API interface {
	// Review authenticates token for audience; it errors unless authenticated
	// and the audience was accepted.
	Review(ctx context.Context, token, audience string) (*Identity, error)
	// CanGetReleases reports whether id may get agentreleases in namespace.
	CanGetReleases(ctx context.Context, id *Identity, namespace string) (bool, error)
	// AgentToken mints a ServiceAccount token for sa in ns, bound to pod.
	AgentToken(ctx context.Context, ns, sa string, pod *corev1.Pod, audience string, seconds int64) (string, error)
}

var ErrUnauthenticated = errors.New("token not authenticated")

type Client struct{ CS kubernetes.Interface }

func (c *Client) Review(ctx context.Context, token, audience string) (*Identity, error) {
	tr, err := c.CS.AuthenticationV1().TokenReviews().Create(ctx, &authnv1.TokenReview{
		Spec: authnv1.TokenReviewSpec{Token: token, Audiences: []string{audience}},
	}, metav1.CreateOptions{})
	if err != nil {
		return nil, err
	}
	st := tr.Status
	if !st.Authenticated || st.Error != "" || !slices.Contains(st.Audiences, audience) {
		return nil, ErrUnauthenticated
	}
	extra := map[string][]string{}
	for k, v := range st.User.Extra {
		extra[k] = v
	}
	return &Identity{Username: st.User.Username, UID: st.User.UID, Groups: st.User.Groups, Extra: extra}, nil
}

func (c *Client) CanGetReleases(ctx context.Context, id *Identity, namespace string) (bool, error) {
	extra := map[string]authzv1.ExtraValue{}
	for k, v := range id.Extra {
		extra[k] = v
	}
	sar, err := c.CS.AuthorizationV1().SubjectAccessReviews().Create(ctx, &authzv1.SubjectAccessReview{
		Spec: authzv1.SubjectAccessReviewSpec{
			User: id.Username, UID: id.UID, Groups: id.Groups, Extra: extra,
			ResourceAttributes: &authzv1.ResourceAttributes{
				Namespace: namespace, Verb: "get", Group: "agentplatform.io", Resource: "agentreleases",
			},
		},
	}, metav1.CreateOptions{})
	if err != nil {
		return false, err
	}
	return sar.Status.Allowed && !sar.Status.Denied, nil
}

func (c *Client) AgentToken(ctx context.Context, ns, sa string, pod *corev1.Pod, audience string, seconds int64) (string, error) {
	tr, err := c.CS.CoreV1().ServiceAccounts(ns).CreateToken(ctx, sa, &authnv1.TokenRequest{
		Spec: authnv1.TokenRequestSpec{
			Audiences:         []string{audience},
			ExpirationSeconds: &seconds,
			BoundObjectRef:    &authnv1.BoundObjectReference{Kind: "Pod", APIVersion: "v1", Name: pod.Name, UID: pod.UID},
		},
	}, metav1.CreateOptions{})
	if err != nil {
		return "", err
	}
	return tr.Status.Token, nil
}
