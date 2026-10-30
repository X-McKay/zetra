package store

import (
	"context"
	"errors"
	"strconv"
	"time"

	"github.com/redis/go-redis/v9"

	"github.com/X-McKay/zetra/platform/aps/internal/policy"
)

// sweep charges every reservation of one ledger whose expiry has passed in full
// into spent. KEYS: hash, zset of decision->expiry, hash of decision->amount.
const luaSweep = `
local function sweep(h, z, a, now)
  local due = redis.call('ZRANGEBYSCORE', z, '-inf', now)
  for _, d in ipairs(due) do
    local amt = tonumber(redis.call('HGET', a, d) or '0')
    redis.call('HINCRBY', h, 'spent', amt)
    redis.call('HINCRBY', h, 'inflight', -amt)
    redis.call('HDEL', a, d)
    redis.call('ZREM', z, d)
  end
end
local function used(h)
  return tonumber(redis.call('HGET', h, 'spent') or '0') + tonumber(redis.call('HGET', h, 'inflight') or '0')
end
`

// reserveScript admits a reservation against the session and agent-day ledgers
// atomically (9.3 step 10).
// KEYS: 1-3 session ledger (hash, res zset, amount hash), 4-6 agent-day ledger, 7 reservation record.
// ARGV: now_ms, amount, session_budget, day_budget, decision, expires_ms, session_ttl_s, day_ttl_s, record_ttl_s.
var reserveScript = redis.NewScript(luaSweep + `
local now, amt = tonumber(ARGV[1]), tonumber(ARGV[2])
sweep(KEYS[1], KEYS[2], KEYS[3], now)
sweep(KEYS[4], KEYS[5], KEYS[6], now)
if used(KEYS[1]) + amt > tonumber(ARGV[3]) or used(KEYS[4]) + amt > tonumber(ARGV[4]) then
  return 0
end
local d = ARGV[5]
for i = 0, 1 do
  local h, z, a, ttl = KEYS[1 + 3 * i], KEYS[2 + 3 * i], KEYS[3 + 3 * i], ARGV[7 + i]
  redis.call('HINCRBY', h, 'inflight', amt)
  redis.call('ZADD', z, ARGV[6], d)
  redis.call('HSET', a, d, amt)
  redis.call('EXPIRE', h, ttl)
  redis.call('EXPIRE', z, ttl)
  redis.call('EXPIRE', a, ttl)
end
redis.call('HSET', KEYS[7], 'sh', KEYS[1], 'sz', KEYS[2], 'sa', KEYS[3], 'ah', KEYS[4], 'az', KEYS[5], 'aa', KEYS[6])
redis.call('EXPIRE', KEYS[7], ARGV[9])
return 1
`)

// settleScript replaces a reservation with actual usage on each ledger that still
// holds it. A reservation already swept was charged in full and stays charged.
// KEYS: 1 reservation record. ARGV: decision, actual.
var settleScript = redis.NewScript(`
local r = redis.call('HMGET', KEYS[1], 'sh', 'sz', 'sa', 'ah', 'az', 'aa')
if not r[1] then return -1 end
local n = 0
for i = 0, 1 do
  local h, z, a = r[1 + 3 * i], r[2 + 3 * i], r[3 + 3 * i]
  if redis.call('ZREM', z, ARGV[1]) == 1 then
    local amt = tonumber(redis.call('HGET', a, ARGV[1]) or '0')
    redis.call('HDEL', a, ARGV[1])
    redis.call('HINCRBY', h, 'inflight', -amt)
    redis.call('HINCRBY', h, 'spent', tonumber(ARGV[2]))
    n = n + 1
  end
end
redis.call('DEL', KEYS[1])
return n
`)

// ledgerScript sweeps one ledger and returns {spent, inflight}. KEYS: hash, zset, amounts. ARGV: now_ms.
var ledgerScript = redis.NewScript(luaSweep + `
sweep(KEYS[1], KEYS[2], KEYS[3], tonumber(ARGV[1]))
return {tonumber(redis.call('HGET', KEYS[1], 'spent') or '0'), tonumber(redis.call('HGET', KEYS[1], 'inflight') or '0')}
`)

// releaseScript drops a session ledger's open reservations. KEYS: hash, zset, amounts.
var releaseScript = redis.NewScript(`
redis.call('DEL', KEYS[2], KEYS[3])
if redis.call('EXISTS', KEYS[1]) == 1 then redis.call('HSET', KEYS[1], 'inflight', 0) end
return 1
`)

// setGenScript sets an active generation that must not decrease. KEYS: gen key. ARGV: gen, ttl_s.
var setGenScript = redis.NewScript(`
local cur = redis.call('GET', KEYS[1])
if cur and tonumber(cur) > tonumber(ARGV[1]) then return 0 end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
return 1
`)

// Redis is the multi-replica Store.
type Redis struct {
	C   redis.UniversalClient
	Now func() time.Time
}

func NewRedis(c redis.UniversalClient) *Redis { return &Redis{C: c, Now: time.Now} }

func ledgerKeys(h string) []string { return []string{h, h + ":res", h + ":amt"} }

func (r *Redis) Ping(ctx context.Context) error { return r.C.Ping(ctx).Err() }

func (r *Redis) PinJTI(ctx context.Context, jti, podUID string, exp time.Time) (string, error) {
	ttl := max(exp.Sub(r.Now()), time.Second)
	ok, err := r.C.SetNX(ctx, "pin:"+jti, podUID, ttl).Result()
	if err != nil {
		return "", err
	}
	if ok {
		return podUID, nil
	}
	return r.C.Get(ctx, "pin:"+jti).Result()
}

func (r *Redis) SetGeneration(ctx context.Context, session string, gen int) error {
	n, err := setGenScript.Run(ctx, r.C, []string{"gen:" + session}, gen, int(SessionStateTTL.Seconds())).Int()
	if err != nil {
		return err
	}
	if n == 0 {
		return ErrConflict
	}
	return nil
}

func (r *Redis) ActiveGeneration(ctx context.Context, session string) (int, bool, error) {
	v, err := r.C.Get(ctx, "gen:"+session).Result()
	if errors.Is(err, redis.Nil) {
		return 0, false, nil
	}
	if err != nil {
		return 0, false, err
	}
	n, err := strconv.Atoi(v)
	return n, err == nil, err
}

func (r *Redis) MarkStopped(ctx context.Context, session string) error {
	return r.C.Set(ctx, "stopped:"+session, "1", SessionStateTTL).Err()
}

func (r *Redis) IsStopped(ctx context.Context, session string) (bool, error) {
	n, err := r.C.Exists(ctx, "stopped:"+session).Result()
	return n == 1, err
}

func (r *Redis) getBytes(ctx context.Context, key string) ([]byte, bool, error) {
	v, err := r.C.Get(ctx, key).Bytes()
	if errors.Is(err, redis.Nil) {
		return nil, false, nil
	}
	return v, err == nil, err
}

func (r *Redis) PutVault(ctx context.Context, session string, blob []byte, ttl time.Duration) (bool, error) {
	ok, err := r.C.SetNX(ctx, "vault:"+session, blob, ttl).Result()
	if err == nil && ok {
		err = r.C.SAdd(ctx, "vault:index", session).Err()
	}
	return ok, err
}

func (r *Redis) SetVault(ctx context.Context, session string, blob []byte, ttl time.Duration) error {
	return r.C.Set(ctx, "vault:"+session, blob, ttl).Err()
}

func (r *Redis) GetVault(ctx context.Context, session string) ([]byte, bool, error) {
	return r.getBytes(ctx, "vault:"+session)
}

func (r *Redis) DeleteVault(ctx context.Context, session string) error {
	if err := r.C.Del(ctx, "vault:"+session).Err(); err != nil {
		return err
	}
	return r.C.SRem(ctx, "vault:index", session).Err()
}

func (r *Redis) CountVault(ctx context.Context) (int64, error) {
	ids, err := r.C.SMembers(ctx, "vault:index").Result()
	if err != nil {
		return 0, err
	}
	var n int64
	for _, id := range ids {
		ok, err := r.C.Exists(ctx, "vault:"+id).Result()
		if err != nil {
			return 0, err
		}
		if ok == 1 {
			n++
		} else {
			r.C.SRem(ctx, "vault:index", id)
		}
	}
	return n, nil
}

func (r *Redis) PutGrant(ctx context.Context, handle string, rec []byte, ttl time.Duration) error {
	return r.C.Set(ctx, "grant:"+handle, rec, ttl).Err()
}

func (r *Redis) GetGrant(ctx context.Context, handle string) ([]byte, bool, error) {
	return r.getBytes(ctx, "grant:"+handle)
}

func (r *Redis) DeleteGrant(ctx context.Context, handle string) error {
	return r.C.Del(ctx, "grant:"+handle).Err()
}

func (r *Redis) PutExchanged(ctx context.Context, session, key string, blob []byte, ttl time.Duration) error {
	k := "xchg:" + session + ":" + key
	_, err := r.C.TxPipelined(ctx, func(p redis.Pipeliner) error {
		p.Set(ctx, k, blob, ttl)
		p.SAdd(ctx, "xchg:idx:"+session, k)
		p.Expire(ctx, "xchg:idx:"+session, SessionStateTTL)
		return nil
	})
	return err
}

func (r *Redis) GetExchanged(ctx context.Context, session, key string) ([]byte, bool, error) {
	return r.getBytes(ctx, "xchg:"+session+":"+key)
}

func (r *Redis) DeleteExchanged(ctx context.Context, session string) error {
	keys, err := r.C.SMembers(ctx, "xchg:idx:"+session).Result()
	if err != nil {
		return err
	}
	return r.C.Del(ctx, append(keys, "xchg:idx:"+session)...).Err()
}

func (r *Redis) Reserve(ctx context.Context, res Reservation) error {
	now := r.Now()
	keys := append(ledgerKeys(SessionLedger(res.Session)), ledgerKeys(AgentLedger(res.AgentID, now))...)
	keys = append(keys, "budget:res:"+res.Decision)
	n, err := reserveScript.Run(ctx, r.C, keys,
		now.UnixMilli(), res.Amount, res.SessionBudget, res.DayBudget, res.Decision,
		now.Add(ReservationTTL).UnixMilli(), int(sessionLedgerTTL.Seconds()), int(dayLedgerTTL.Seconds()),
		int(sessionLedgerTTL.Seconds())).Int()
	if err != nil {
		return err
	}
	if n == 0 {
		return policy.ErrBudget
	}
	return nil
}

func (r *Redis) Settle(ctx context.Context, decision string, actual int64) (bool, error) {
	n, err := settleScript.Run(ctx, r.C, []string{"budget:res:" + decision}, decision, actual).Int()
	if err != nil {
		return false, err
	}
	return n > 0, nil
}

func (r *Redis) ReleaseSession(ctx context.Context, session string) error {
	return releaseScript.Run(ctx, r.C, ledgerKeys(SessionLedger(session))).Err()
}

func (r *Redis) Ledger(ctx context.Context, key string) (int64, int64, error) {
	v, err := ledgerScript.Run(ctx, r.C, ledgerKeys(key), r.Now().UnixMilli()).Int64Slice()
	if err != nil {
		return 0, 0, err
	}
	return v[0], v[1], nil
}

func (r *Redis) TouchSession(ctx context.Context, session, tenant, agent, handle string) error {
	k := "session:" + session
	_, err := r.C.TxPipelined(ctx, func(p redis.Pipeliner) error {
		if tenant != "" {
			p.HSetNX(ctx, k, "tenant", tenant)
		}
		if agent != "" {
			p.HSetNX(ctx, k, "agent", agent)
		}
		p.HSetNX(ctx, k, "since", r.Now().UnixMilli())
		if handle != "" {
			p.HSet(ctx, k, "grant_handle", handle)
		}
		p.Expire(ctx, k, SessionStateTTL)
		p.ZAddNX(ctx, "sessions", redis.Z{Score: float64(r.Now().UnixMilli()), Member: session})
		return nil
	})
	return err
}

func (r *Redis) SessionTenant(ctx context.Context, session string) (string, bool, error) {
	v, err := r.C.HGet(ctx, "session:"+session, "tenant").Result()
	if errors.Is(err, redis.Nil) {
		return "", false, nil
	}
	return v, err == nil && v != "", err
}

func (r *Redis) ListSessions(ctx context.Context, olderThan time.Time) ([]SessionInfo, error) {
	ids, err := r.C.ZRangeByScore(ctx, "sessions", &redis.ZRangeBy{Min: "-inf", Max: strconv.FormatInt(olderThan.UnixMilli(), 10)}).Result()
	if err != nil {
		return nil, err
	}
	now := r.Now().UnixMilli()
	var out []SessionInfo
	for _, id := range ids {
		h, err := r.C.HGetAll(ctx, "session:"+id).Result()
		if err != nil {
			return nil, err
		}
		since, _ := strconv.ParseInt(h["since"], 10, 64)
		info := SessionInfo{SessionID: id, Tenant: h["tenant"], Agent: h["agent"], Since: time.UnixMilli(since).UTC()}
		if g, ok, err := r.ActiveGeneration(ctx, id); err != nil {
			return nil, err
		} else if ok {
			info.Generation = &g
		}
		if n, err := r.C.Exists(ctx, "vault:"+id).Result(); err != nil {
			return nil, err
		} else {
			info.HasGrant = n == 1
		}
		if gh := h["grant_handle"]; info.HasGrant && gh != "" {
			info.GrantHandle = &gh
		}
		if info.Stopped, err = r.IsStopped(ctx, id); err != nil {
			return nil, err
		}
		if info.Reservations, err = r.C.ZCount(ctx, SessionLedger(id)+":res", strconv.FormatInt(now, 10), "+inf").Result(); err != nil {
			return nil, err
		}
		if info.Generation == nil && !info.HasGrant && info.Reservations == 0 {
			r.C.ZRem(ctx, "sessions", id) // nothing held any more
			continue
		}
		out = append(out, info)
	}
	return out, nil
}
