package main

import (
	"context"
	"fmt"
	"time"

	corev1 "k8s.io/api/core/v1"

	"github.com/X-McKay/zetra/platform/aps/internal/decision"
	"github.com/X-McKay/zetra/platform/aps/internal/exchange"
	"github.com/X-McKay/zetra/platform/aps/internal/jwtverify"
	"github.com/X-McKay/zetra/platform/aps/internal/podindex"
	"github.com/X-McKay/zetra/platform/aps/internal/policy"
	"github.com/X-McKay/zetra/platform/aps/internal/registry"
	"github.com/X-McKay/zetra/platform/aps/internal/store"
	"github.com/X-McKay/zetra/platform/aps/internal/vault"
)

// Default budgets when a bundle omits them (3.7).
const (
	defaultSessionBudget = 200_000
	defaultDayBudget     = 2_000_000
)

func buildDeps(v *jwtverify.Verifier, pods *podindex.Index, reg *registry.Registry, st store.Store,
	entries *vault.Entries, x *exchange.Exchanger, catalogue map[string]int) decision.Deps {
	return decision.Deps{
		Verify: func(ctx context.Context, bearer string) (*policy.AgentClaims, error) {
			c, err := v.Verify(ctx, bearer, policy.AgentGatewayAud, false)
			if err != nil {
				return nil, err
			}
			return &policy.AgentClaims{
				Sub: c.Sub, JTI: c.JTI, AgentID: c.AgentID, Tenant: c.Tenant, SpiffeID: c.SpiffeID,
				ClientID: c.Azp, ClientType: c.ClientType, Exp: c.Expiry, Raw: bearer,
			}, nil
		},
		PodByIP: pods.PodByIP,
		PinOK: func(ctx context.Context, jti, podUID string, exp time.Time) (bool, error) {
			pinned, err := st.PinJTI(ctx, jti, podUID, exp)
			return err == nil && pinned == podUID, err
		},
		ActiveGen: st.ActiveGeneration,
		Stopped:   st.IsStopped,
		ReleaseOK: reg.ReleaseOK,
		Bundle:    reg.Bundle,
		Reserve: func(ctx context.Context, session, agentID, backend, decision string, b *policy.Bundle) (int, error) {
			amount, ok := catalogue[backend]
			if !ok {
				return 0, fmt.Errorf("backend %q not in catalogue", backend)
			}
			sb, db := b.Budgets.TokensPerSession, b.Budgets.TokensPerDay
			if sb <= 0 {
				sb = defaultSessionBudget
			}
			if db <= 0 {
				db = defaultDayBudget
			}
			err := st.Reserve(ctx, store.Reservation{
				Decision: decision, Session: session, AgentID: agentID,
				Amount: int64(amount), SessionBudget: sb, DayBudget: db,
			})
			return amount, err
		},
		HasGrant:  entries.Usable,
		Principal: entries.Principal,
		Exchange: func(ctx context.Context, mode, session, backend, resource, actor string, pod *corev1.Pod) (*policy.Exchanged, error) {
			return x.Exchange(ctx, mode, session, backend, resource, actor, pod)
		},
		Now: time.Now,
	}
}
