// Package metrics defines the APS metrics of 13.5. Every method is nil-safe so
// tests can pass a nil *Metrics.
package metrics

import (
	"strconv"
	"time"

	"github.com/prometheus/client_golang/prometheus"
)

type Metrics struct {
	decisions        *prometheus.CounterVec
	checkDuration    *prometheus.HistogramVec
	exchangeDuration *prometheus.HistogramVec
	exchangeFailures *prometheus.CounterVec
	vaultEntries     prometheus.Gauge
	settled          *prometheus.CounterVec
}

func New(reg prometheus.Registerer) *Metrics {
	m := &Metrics{
		decisions: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "aps_decisions_total", Help: "APS decisions by reason.",
		}, []string{"reason", "route", "tenant"}),
		checkDuration: prometheus.NewHistogramVec(prometheus.HistogramOpts{
			Name: "aps_check_duration_seconds", Help: "APS check latency.", Buckets: prometheus.DefBuckets,
		}, []string{"route"}),
		exchangeDuration: prometheus.NewHistogramVec(prometheus.HistogramOpts{
			Name: "aps_exchange_duration_seconds", Help: "Token exchange latency.", Buckets: prometheus.DefBuckets,
		}, []string{"mode", "cached"}),
		exchangeFailures: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "aps_exchange_failures_total", Help: "Token exchange failures.",
		}, []string{"mode", "error"}),
		vaultEntries: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "aps_vault_entries", Help: "Vault entries held.",
		}),
		settled: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "aps_budget_settled_total", Help: "Budget reservations settled from the access-log stream.",
		}, []string{"result"}),
	}
	reg.MustRegister(m.decisions, m.checkDuration, m.exchangeDuration, m.exchangeFailures, m.vaultEntries, m.settled)
	return m
}

func (m *Metrics) Decision(reason, route, tenant string, d time.Duration) {
	if m == nil {
		return
	}
	m.decisions.WithLabelValues(reason, route, tenant).Inc()
	m.checkDuration.WithLabelValues(route).Observe(d.Seconds())
}

func (m *Metrics) Exchange(mode string, cached bool, d time.Duration) {
	if m == nil {
		return
	}
	m.exchangeDuration.WithLabelValues(mode, strconv.FormatBool(cached)).Observe(d.Seconds())
}

func (m *Metrics) ExchangeFailure(mode, class string) {
	if m == nil {
		return
	}
	m.exchangeFailures.WithLabelValues(mode, class).Inc()
}

func (m *Metrics) VaultEntries(n int64) {
	if m == nil {
		return
	}
	m.vaultEntries.Set(float64(n))
}

func (m *Metrics) Settled(result string) {
	if m == nil {
		return
	}
	m.settled.WithLabelValues(result).Inc()
}
