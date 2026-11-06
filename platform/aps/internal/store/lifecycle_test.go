package store

import (
	"context"
	"testing"
	"time"
)

func TestLifecycleObservationTracksActualGrantAfterVaultStop(t *testing.T) {
	backends(t, func(t *testing.T, s Store, c *clock) {
		ctx := context.Background()
		if _, ok, err := s.ObserveLifecycle(ctx, sess); err != nil || ok {
			t.Fatalf("unknown session accepted: %v %v", ok, err)
		}
		_ = s.TouchSession(ctx, sess, "tenant-test", "conformance-lifecycle", "private-handle")
		_ = s.SetGeneration(ctx, sess, 1)
		_, _ = s.PutVault(ctx, sess, []byte("private-vault"), time.Hour)
		_ = s.PutGrant(ctx, "private-handle", []byte("private-grant"), time.Hour)
		got, ok, err := s.ObserveLifecycle(ctx, sess)
		if err != nil || !ok || got.Tenant != "tenant-test" || got.Agent != "conformance-lifecycle" || got.Generation != 1 || !got.GenerationPresent || !got.GrantPresent || !got.VaultPresent || got.GrantTTLSeconds < 3500 || got.VaultTTLSeconds < 3500 || got.Stopped || got.CleanupPending {
			t.Fatalf("initial: %+v %v %v", got, ok, err)
		}
		_ = s.MarkStopped(ctx, sess)
		_ = s.DeleteVault(ctx, sess)
		got, _, _ = s.ObserveLifecycle(ctx, sess)
		if got.Generation != 0 || got.GenerationPresent || !got.GrantPresent || got.VaultPresent || !got.Stopped || !got.CleanupPending {
			t.Fatalf("partial: %+v", got)
		}
		_ = s.CompleteStop(ctx, sess)
		_ = s.DeleteGrant(ctx, "private-handle")
		rows, _ := s.ListSessions(ctx, c.Now().Add(time.Hour))
		if len(rows) != 0 {
			t.Fatal("cleaned session remains discoverable")
		}
		got, ok, err = s.ObserveLifecycle(ctx, sess)
		if err != nil || !ok || got.Tenant != "tenant-test" || got.Agent != "conformance-lifecycle" || got.Generation != 0 || got.GenerationPresent || got.GrantPresent || got.VaultPresent || !got.Stopped || got.CleanupPending {
			t.Fatalf("final: %+v %v %v", got, ok, err)
		}
	})
}

func TestLifecycleMemoryOwnerExpiresAndCannotBeReassigned(t *testing.T) {
	ctx := context.Background()
	s := NewMemory()
	now := time.Now()
	s.Now = func() time.Time { return now }
	_ = s.TouchSession(ctx, sess, "tenant-test", "conformance-lifecycle", "")
	_ = s.TouchSession(ctx, sess, "other", "attacker", "")
	got, ok, _ := s.ObserveLifecycle(ctx, sess)
	if !ok || got.Tenant != "tenant-test" || got.Agent != "conformance-lifecycle" {
		t.Fatalf("owner changed: %+v", got)
	}
	now = now.Add(SessionStateTTL)
	if _, ok, _ := s.ObserveLifecycle(ctx, sess); ok {
		t.Fatal("expired metadata accepted as final cleanup proof")
	}
}

func TestLifecycleDoesNotConfuseZeroGenerationWithAbsentKey(t *testing.T) {
	backends(t, func(t *testing.T, s Store, c *clock) {
		ctx := context.Background()
		_ = s.TouchSession(ctx, sess, "tenant-test", "conformance-lifecycle", "")
		_ = s.SetGeneration(ctx, sess, 0)
		got, known, err := s.ObserveLifecycle(ctx, sess)
		if err != nil || !known || !got.GenerationPresent {
			t.Fatalf("active zero key hidden: %+v %v", got, err)
		}
	})
}
