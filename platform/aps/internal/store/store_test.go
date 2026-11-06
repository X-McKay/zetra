package store

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"

	"github.com/X-McKay/zetra/platform/aps/internal/policy"
)

type clock struct {
	mu sync.Mutex
	t  time.Time
}

func (c *clock) Now() time.Time      { c.mu.Lock(); defer c.mu.Unlock(); return c.t }
func (c *clock) Add(d time.Duration) { c.mu.Lock(); c.t = c.t.Add(d); c.mu.Unlock() }

// backends runs f against the memory store and the Redis store (miniredis).
func backends(t *testing.T, f func(t *testing.T, s Store, c *clock)) {
	t.Run("memory", func(t *testing.T) {
		c := &clock{t: time.Date(2026, 10, 30, 12, 0, 0, 0, time.UTC)}
		m := NewMemory()
		m.Now = c.Now
		f(t, m, c)
	})
	t.Run("redis", func(t *testing.T) {
		c := &clock{t: time.Date(2026, 10, 30, 12, 0, 0, 0, time.UTC)}
		mr := miniredis.RunT(t)
		r := NewRedis(redis.NewClient(&redis.Options{Addr: mr.Addr()}))
		r.Now = c.Now
		f(t, r, c)
	})
}

const sess = "6f1d2c3e-0000-4000-8000-000000000001"

func res(decision string, amount, sb, db int64) Reservation {
	return Reservation{Decision: decision, Session: sess, AgentID: "tenant-a/investigator", Amount: amount, SessionBudget: sb, DayBudget: db}
}

// T13b logic: ten concurrent reserves admit exactly floor(remaining / reserve).
func TestT13bConcurrentReservesAdmitFloorRemaining(t *testing.T) {
	backends(t, func(t *testing.T, s Store, c *clock) {
		ctx := context.Background()
		// Spend 100 of a 1,000 budget first.
		if err := s.Reserve(ctx, res("warm", 300, 1000, 1_000_000)); err != nil {
			t.Fatal(err)
		}
		if ok, err := s.Settle(ctx, "warm", 100); err != nil || !ok {
			t.Fatalf("settle: %v %v", ok, err)
		}
		var admitted, refused atomic.Int32
		var wg sync.WaitGroup
		for i := 0; i < 10; i++ {
			wg.Add(1)
			go func(i int) {
				defer wg.Done()
				err := s.Reserve(ctx, res("d"+string(rune('a'+i)), 300, 1000, 1_000_000))
				switch {
				case err == nil:
					admitted.Add(1)
				case errors.Is(err, policy.ErrBudget):
					refused.Add(1)
				default:
					t.Error(err)
				}
			}(i)
		}
		wg.Wait()
		if admitted.Load() != 3 || refused.Load() != 7 { // floor((1000-100)/300) = 3
			t.Fatalf("admitted %d refused %d, want 3 and 7", admitted.Load(), refused.Load())
		}
		spent, inflight, err := s.Ledger(ctx, SessionLedger(sess))
		if err != nil || spent != 100 || inflight != 900 {
			t.Fatalf("ledger spent=%d inflight=%d err=%v", spent, inflight, err)
		}
	})
}

func TestUnsettledReservationChargedInFullAfterExpiry(t *testing.T) {
	backends(t, func(t *testing.T, s Store, c *clock) {
		ctx := context.Background()
		if err := s.Reserve(ctx, res("d1", 400, 1000, 5000)); err != nil {
			t.Fatal(err)
		}
		c.Add(129 * time.Second)
		if spent, inflight, _ := s.Ledger(ctx, SessionLedger(sess)); spent != 0 || inflight != 400 {
			t.Fatalf("before expiry spent=%d inflight=%d", spent, inflight)
		}
		c.Add(2 * time.Second)
		for _, key := range []string{SessionLedger(sess), AgentLedger("tenant-a/investigator", c.Now())} {
			if spent, inflight, _ := s.Ledger(ctx, key); spent != 400 || inflight != 0 {
				t.Fatalf("%s after expiry spent=%d inflight=%d", key, spent, inflight)
			}
		}
		// A late settlement does not refund the charge.
		if ok, _ := s.Settle(ctx, "d1", 10); ok {
			t.Fatal("late settlement applied")
		}
		if spent, _, _ := s.Ledger(ctx, SessionLedger(sess)); spent != 400 {
			t.Fatalf("spent=%d after late settlement", spent)
		}
		// The expired charge counts against new reservations.
		if err := s.Reserve(ctx, res("d2", 700, 1000, 5000)); !errors.Is(err, policy.ErrBudget) {
			t.Fatalf("reserve over budget: %v", err)
		}
	})
}

// Settlement must not refund a hold merely because no read/reserve swept it yet.
func TestLateSettlementBeforeSweepStaysFullySpent(t *testing.T) {
	for _, delay := range []time.Duration{ReservationTTL, ReservationTTL + time.Second} {
		t.Run(delay.String(), func(t *testing.T) {
			backends(t, func(t *testing.T, s Store, c *clock) {
				ctx := context.Background()
				if err := s.Reserve(ctx, res("late", 400, 1000, 5000)); err != nil {
					t.Fatal(err)
				}
				c.Add(delay)
				if ok, err := s.Settle(ctx, "late", 10); err != nil || ok {
					t.Fatalf("late settlement: %v %v", ok, err)
				}
				for _, key := range []string{SessionLedger(sess), AgentLedger("tenant-a/investigator", c.Now())} {
					if spent, inflight, err := s.Ledger(ctx, key); err != nil || spent != 400 || inflight != 0 {
						t.Fatalf("late ledger = %d/%d, %v", spent, inflight, err)
					}
				}
			})
		})
	}
}

func TestSettlementReplacesReservation(t *testing.T) {
	backends(t, func(t *testing.T, s Store, c *clock) {
		ctx := context.Background()
		if err := s.Reserve(ctx, res("d1", 500, 1000, 5000)); err != nil {
			t.Fatal(err)
		}
		if ok, err := s.Settle(ctx, "d1", 120); err != nil || !ok {
			t.Fatalf("settle %v %v", ok, err)
		}
		for _, key := range []string{SessionLedger(sess), AgentLedger("tenant-a/investigator", c.Now())} {
			if spent, inflight, _ := s.Ledger(ctx, key); spent != 120 || inflight != 0 {
				t.Fatalf("%s spent=%d inflight=%d", key, spent, inflight)
			}
		}
		if ok, _ := s.Settle(ctx, "d1", 120); ok {
			t.Fatal("double settlement")
		}
		if ok, _ := s.Settle(ctx, "unknown", 1); ok {
			t.Fatal("settled unknown decision")
		}
	})
}

func TestAgentDayBudgetSharedAcrossSessions(t *testing.T) {
	backends(t, func(t *testing.T, s Store, c *clock) {
		ctx := context.Background()
		a := Reservation{Decision: "a", Session: sess, AgentID: "tenant-a/x", Amount: 600, SessionBudget: 1000, DayBudget: 1000}
		b := Reservation{Decision: "b", Session: "6f1d2c3e-0000-4000-8000-000000000002", AgentID: "tenant-a/x", Amount: 600, SessionBudget: 1000, DayBudget: 1000}
		if err := s.Reserve(ctx, a); err != nil {
			t.Fatal(err)
		}
		if err := s.Reserve(ctx, b); !errors.Is(err, policy.ErrBudget) {
			t.Fatalf("second session over day budget: %v", err)
		}
	})
}

func TestReleaseSessionDropsOpenReservations(t *testing.T) {
	backends(t, func(t *testing.T, s Store, c *clock) {
		ctx := context.Background()
		_ = s.Reserve(ctx, res("d1", 500, 1000, 5000))
		if err := s.ReleaseSession(ctx, sess); err != nil {
			t.Fatal(err)
		}
		if spent, inflight, _ := s.Ledger(ctx, SessionLedger(sess)); spent != 0 || inflight != 0 {
			t.Fatalf("spent=%d inflight=%d", spent, inflight)
		}
		// The agent-day reservation still settles.
		if ok, _ := s.Settle(ctx, "d1", 50); !ok {
			t.Fatal("agent-day reservation not settled")
		}
		if spent, _, _ := s.Ledger(ctx, AgentLedger("tenant-a/investigator", c.Now())); spent != 50 {
			t.Fatalf("agent spent=%d", spent)
		}
	})
}

func TestGenerationStrictlyIncreasing(t *testing.T) {
	backends(t, func(t *testing.T, s Store, c *clock) {
		ctx := context.Background()
		if _, ok, _ := s.ActiveGeneration(ctx, sess); ok {
			t.Fatal("generation before set")
		}
		for _, g := range []int{1, 1, 2} { // a repeat of the same value is accepted
			if err := s.SetGeneration(ctx, sess, g); err != nil {
				t.Fatalf("set %d: %v", g, err)
			}
		}
		if err := s.SetGeneration(ctx, sess, 1); !errors.Is(err, ErrConflict) {
			t.Fatalf("decrease: %v", err)
		}
		if g, ok, _ := s.ActiveGeneration(ctx, sess); !ok || g != 2 {
			t.Fatalf("active %d %v", g, ok)
		}
	})
}

func TestPinFirstWriterWins(t *testing.T) {
	backends(t, func(t *testing.T, s Store, c *clock) {
		ctx := context.Background()
		exp := c.Now().Add(300 * time.Second)
		if p, _ := s.PinJTI(ctx, "j", "pod-1", exp); p != "pod-1" {
			t.Fatal(p)
		}
		if p, _ := s.PinJTI(ctx, "j", "pod-2", exp); p != "pod-1" {
			t.Fatalf("re-pinned to %s", p)
		}
	})
}

func TestStopRemovesGenerationAndFencesLateActivities(t *testing.T) {
	backends(t, func(t *testing.T, s Store, c *clock) {
		ctx := context.Background()
		if err := s.SetGeneration(ctx, sess, 1); err != nil {
			t.Fatal(err)
		}
		if err := s.MarkStopped(ctx, sess); err != nil {
			t.Fatal(err)
		}
		if g, ok, err := s.ActiveGeneration(ctx, sess); err != nil || ok {
			t.Fatalf("stopped generation retained: %d %v %v", g, ok, err)
		}
		for _, generation := range []int{1, 2} {
			if err := s.SetGeneration(ctx, sess, generation); !errors.Is(err, ErrConflict) {
				t.Fatalf("late generation %d: %v", generation, err)
			}
		}
		if stopped, err := s.IsStopped(ctx, sess); err != nil || !stopped {
			t.Fatalf("stop tombstone lost: %v %v", stopped, err)
		}
	})
}

func TestStopAndGenerationRaceCannotLeaveActiveGeneration(t *testing.T) {
	backends(t, func(t *testing.T, s Store, c *clock) {
		ctx := context.Background()
		var wg sync.WaitGroup
		start := make(chan struct{})
		for i := 0; i < 20; i++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				<-start
				if err := s.SetGeneration(ctx, sess, 2); err != nil && !errors.Is(err, ErrConflict) {
					t.Error(err)
				}
			}()
		}
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			if err := s.MarkStopped(ctx, sess); err != nil {
				t.Error(err)
			}
		}()
		close(start)
		wg.Wait()
		if _, ok, err := s.ActiveGeneration(ctx, sess); err != nil || ok {
			t.Fatalf("generation survived stop race: %v %v", ok, err)
		}
	})
}

func TestStoppedSessionCannotRegainVaultOrExchangeState(t *testing.T) {
	backends(t, func(t *testing.T, s Store, c *clock) {
		ctx := context.Background()
		if err := s.MarkStopped(ctx, sess); err != nil {
			t.Fatal(err)
		}
		if _, err := s.PutVault(ctx, sess, []byte("late deposit"), time.Hour); err == nil {
			t.Fatal("stopped session accepted a new vault deposit")
		}
		if err := s.SetVault(ctx, sess, []byte("late refresh"), time.Hour); err == nil {
			t.Fatal("stopped session accepted a late vault refresh")
		}
		if err := s.PutExchanged(ctx, sess, "backend", []byte("late exchange"), time.Hour); err == nil {
			t.Fatal("stopped session accepted a late exchanged token")
		}
		if _, held, _ := s.GetVault(ctx, sess); held {
			t.Fatal("vault state recreated after stop")
		}
		if _, held, _ := s.GetExchanged(ctx, sess, "backend"); held {
			t.Fatal("exchange state recreated after stop")
		}
	})
}

func TestPendingStopSurvivesDiscoveryUntilCompletionReceipt(t *testing.T) {
	backends(t, func(t *testing.T, s Store, c *clock) {
		ctx := context.Background()
		_ = s.TouchSession(ctx, sess, "tenant-a", "investigator", "")
		_ = s.SetGeneration(ctx, sess, 1)
		if err := s.MarkStopped(ctx, sess); err != nil {
			t.Fatal(err)
		}
		list, err := s.ListSessions(ctx, c.Now())
		if err != nil || len(list) != 1 || !list[0].CleanupPending || list[0].Generation != nil {
			t.Fatalf("pending stop receipt lost: %+v %v", list, err)
		}
		if err := s.CompleteStop(ctx, sess); err != nil {
			t.Fatal(err)
		}
		list, err = s.ListSessions(ctx, c.Now())
		if err != nil || len(list) != 0 {
			t.Fatalf("completed stop retained: %+v %v", list, err)
		}
	})
}

func TestStopAndCredentialWriteRaceLeavesNoCredentialState(t *testing.T) {
	backends(t, func(t *testing.T, s Store, c *clock) {
		ctx := context.Background()
		var wg sync.WaitGroup
		start := make(chan struct{})
		for i := 0; i < 20; i++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				<-start
				_, e1 := s.PutVault(ctx, sess, []byte("deposit"), time.Hour)
				e2 := s.SetVault(ctx, sess, []byte("refresh"), time.Hour)
				e3 := s.PutExchanged(ctx, sess, "backend", []byte("exchange"), time.Hour)
				for _, err := range []error{e1, e2, e3} {
					if err != nil && !errors.Is(err, ErrStopped) {
						t.Error(err)
					}
				}
			}()
		}
		close(start)
		if err := s.MarkStopped(ctx, sess); err != nil {
			t.Fatal(err)
		}
		if err := errors.Join(s.DeleteVault(ctx, sess), s.DeleteExchanged(ctx, sess)); err != nil {
			t.Fatal(err)
		}
		wg.Wait()
		if _, held, _ := s.GetVault(ctx, sess); held {
			t.Fatal("vault resurrected by in-flight write")
		}
		if _, held, _ := s.GetExchanged(ctx, sess, "backend"); held {
			t.Fatal("cache resurrected by in-flight write")
		}
	})
}

func TestVaultStoppedAndSessions(t *testing.T) {
	backends(t, func(t *testing.T, s Store, c *clock) {
		ctx := context.Background()
		if ok, _ := s.PutVault(ctx, sess, []byte("x"), time.Hour); !ok {
			t.Fatal("put")
		}
		if ok, _ := s.PutVault(ctx, sess, []byte("y"), time.Hour); ok {
			t.Fatal("second put succeeded")
		}
		if n, _ := s.CountVault(ctx); n != 1 {
			t.Fatalf("count %d", n)
		}
		_ = s.TouchSession(ctx, sess, "tenant-a", "investigator", "gh_1")
		_ = s.SetGeneration(ctx, sess, 1)
		c.Add(20 * time.Minute)
		list, err := s.ListSessions(ctx, c.Now().Add(-15*time.Minute))
		if err != nil || len(list) != 1 || list[0].Tenant != "tenant-a" || list[0].Agent != "investigator" || list[0].GrantHandle == nil || *list[0].GrantHandle != "gh_1" || !list[0].HasGrant {
			t.Fatalf("list %+v %v", list, err)
		}
		if tn, ok, _ := s.SessionTenant(ctx, sess); !ok || tn != "tenant-a" {
			t.Fatal("tenant")
		}
		_ = s.MarkStopped(ctx, sess)
		if st, _ := s.IsStopped(ctx, sess); !st {
			t.Fatal("not stopped")
		}
		_ = s.PutExchanged(ctx, sess, "k|agent", []byte("t"), time.Minute)
		_ = s.DeleteExchanged(ctx, sess)
		if _, ok, _ := s.GetExchanged(ctx, sess, "k|agent"); ok {
			t.Fatal("exchanged survived delete")
		}
		_ = s.DeleteVault(ctx, sess)
		if _, ok, _ := s.GetVault(ctx, sess); ok {
			t.Fatal("vault survived delete")
		}
	})
}
