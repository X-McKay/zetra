package store

import (
	"context"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/X-McKay/zetra/platform/aps/internal/policy"
)

type item struct {
	val []byte
	exp time.Time // zero means no expiry
}

type ledger struct {
	spent, inflight int64
	res             map[string]memRes // decision -> reservation
}

type memRes struct {
	amount int64
	exp    time.Time
}

type resRecord struct {
	session, agent string
	amount         int64
}

type memSession struct {
	tenant, agent, handle string
	since                 time.Time
}

// Memory is a single-replica Store.
type Memory struct {
	Now func() time.Time

	mu       sync.Mutex
	kv       map[string]item
	ledgers  map[string]*ledger
	records  map[string]resRecord
	sessions map[string]*memSession
}

func NewMemory() *Memory {
	return &Memory{
		Now: time.Now, kv: map[string]item{}, ledgers: map[string]*ledger{},
		records: map[string]resRecord{}, sessions: map[string]*memSession{},
	}
}

func (m *Memory) Ping(context.Context) error { return nil }

func (m *Memory) get(k string) ([]byte, bool) {
	it, ok := m.kv[k]
	if !ok {
		return nil, false
	}
	if !it.exp.IsZero() && !m.Now().Before(it.exp) {
		delete(m.kv, k)
		return nil, false
	}
	return it.val, true
}

func (m *Memory) set(k string, v []byte, ttl time.Duration) {
	it := item{val: append([]byte(nil), v...)}
	if ttl > 0 {
		it.exp = m.Now().Add(ttl)
	}
	m.kv[k] = it
}

func (m *Memory) PinJTI(_ context.Context, jti, podUID string, exp time.Time) (string, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if v, ok := m.get("pin:" + jti); ok {
		return string(v), nil
	}
	m.set("pin:"+jti, []byte(podUID), max(exp.Sub(m.Now()), time.Second))
	return podUID, nil
}

func (m *Memory) SetGeneration(_ context.Context, session string, gen int) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if v, ok := m.get("gen:" + session); ok && atoi(string(v)) > gen {
		return ErrConflict
	}
	m.set("gen:"+session, []byte(itoa(gen)), SessionStateTTL)
	return nil
}

func (m *Memory) ActiveGeneration(_ context.Context, session string) (int, bool, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	v, ok := m.get("gen:" + session)
	if !ok {
		return 0, false, nil
	}
	return atoi(string(v)), true, nil
}

func (m *Memory) MarkStopped(_ context.Context, session string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.set("stopped:"+session, []byte("1"), SessionStateTTL)
	return nil
}

func (m *Memory) IsStopped(_ context.Context, session string) (bool, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	_, ok := m.get("stopped:" + session)
	return ok, nil
}

func (m *Memory) PutVault(_ context.Context, session string, blob []byte, ttl time.Duration) (bool, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, ok := m.get("vault:" + session); ok {
		return false, nil
	}
	m.set("vault:"+session, blob, ttl)
	return true, nil
}

func (m *Memory) SetVault(_ context.Context, session string, blob []byte, ttl time.Duration) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.set("vault:"+session, blob, ttl)
	return nil
}

func (m *Memory) GetVault(_ context.Context, session string) ([]byte, bool, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	v, ok := m.get("vault:" + session)
	return v, ok, nil
}

func (m *Memory) DeleteVault(_ context.Context, session string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	delete(m.kv, "vault:"+session)
	return nil
}

func (m *Memory) CountVault(context.Context) (int64, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	var n int64
	for k := range m.kv {
		if strings.HasPrefix(k, "vault:") {
			if _, ok := m.get(k); ok {
				n++
			}
		}
	}
	return n, nil
}

func (m *Memory) PutGrant(_ context.Context, handle string, rec []byte, ttl time.Duration) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.set("grant:"+handle, rec, ttl)
	return nil
}

func (m *Memory) GetGrant(_ context.Context, handle string) ([]byte, bool, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	v, ok := m.get("grant:" + handle)
	return v, ok, nil
}

func (m *Memory) DeleteGrant(_ context.Context, handle string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	delete(m.kv, "grant:"+handle)
	return nil
}

func (m *Memory) PutExchanged(_ context.Context, session, key string, blob []byte, ttl time.Duration) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.set("xchg:"+session+":"+key, blob, ttl)
	return nil
}

func (m *Memory) GetExchanged(_ context.Context, session, key string) ([]byte, bool, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	v, ok := m.get("xchg:" + session + ":" + key)
	return v, ok, nil
}

func (m *Memory) DeleteExchanged(_ context.Context, session string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	for k := range m.kv {
		if strings.HasPrefix(k, "xchg:"+session+":") {
			delete(m.kv, k)
		}
	}
	return nil
}

func (m *Memory) ledger(key string) *ledger {
	l, ok := m.ledgers[key]
	if !ok {
		l = &ledger{res: map[string]memRes{}}
		m.ledgers[key] = l
	}
	now := m.Now()
	for d, r := range l.res { // unsettled reservations are charged in full
		if !now.Before(r.exp) {
			l.spent += r.amount
			l.inflight -= r.amount
			delete(l.res, d)
		}
	}
	return l
}

func (m *Memory) Reserve(_ context.Context, r Reservation) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	now := m.Now()
	s, a := m.ledger(SessionLedger(r.Session)), m.ledger(AgentLedger(r.AgentID, now))
	if s.spent+s.inflight+r.Amount > r.SessionBudget || a.spent+a.inflight+r.Amount > r.DayBudget {
		return policy.ErrBudget
	}
	exp := now.Add(ReservationTTL)
	for _, l := range []*ledger{s, a} {
		l.inflight += r.Amount
		l.res[r.Decision] = memRes{amount: r.Amount, exp: exp}
	}
	m.records[r.Decision] = resRecord{session: SessionLedger(r.Session), agent: AgentLedger(r.AgentID, now), amount: r.Amount}
	return nil
}

func (m *Memory) Settle(_ context.Context, decision string, actual int64) (bool, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	rec, ok := m.records[decision]
	if !ok {
		return false, nil
	}
	delete(m.records, decision)
	settled := false
	for _, key := range []string{rec.session, rec.agent} {
		l := m.ledger(key)
		if r, ok := l.res[decision]; ok {
			l.inflight -= r.amount
			l.spent += actual
			delete(l.res, decision)
			settled = true
		}
	}
	return settled, nil
}

func (m *Memory) ReleaseSession(_ context.Context, session string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	l := m.ledger(SessionLedger(session))
	l.res, l.inflight = map[string]memRes{}, 0
	return nil
}

func (m *Memory) Ledger(_ context.Context, key string) (int64, int64, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	l := m.ledger(key)
	return l.spent, l.inflight, nil
}

func (m *Memory) TouchSession(_ context.Context, session, tenant, agent, handle string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	s, ok := m.sessions[session]
	if !ok {
		s = &memSession{since: m.Now()}
		m.sessions[session] = s
	}
	if s.tenant == "" {
		s.tenant = tenant
	}
	if s.agent == "" {
		s.agent = agent
	}
	if handle != "" {
		s.handle = handle
	}
	return nil
}

func (m *Memory) SessionTenant(_ context.Context, session string) (string, bool, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	s, ok := m.sessions[session]
	if !ok || s.tenant == "" {
		return "", false, nil
	}
	return s.tenant, true, nil
}

func (m *Memory) ListSessions(_ context.Context, olderThan time.Time) ([]SessionInfo, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	var out []SessionInfo
	for id, s := range m.sessions {
		if s.since.After(olderThan) {
			continue
		}
		info := SessionInfo{SessionID: id, Tenant: s.tenant, Agent: s.agent, Since: s.since}
		if _, held := m.get("vault:" + id); held && s.handle != "" {
			h := s.handle
			info.GrantHandle = &h
		}
		if v, ok := m.get("gen:" + id); ok {
			g := atoi(string(v))
			info.Generation = &g
		}
		_, info.HasGrant = m.get("vault:" + id)
		_, info.Stopped = m.get("stopped:" + id)
		info.Reservations = int64(len(m.ledger(SessionLedger(id)).res))
		if info.Generation == nil && !info.HasGrant && info.Reservations == 0 {
			delete(m.sessions, id) // nothing held any more
			continue
		}
		out = append(out, info)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Since.Before(out[j].Since) })
	return out, nil
}
