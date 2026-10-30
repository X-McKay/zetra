// Package settlement is the OTLP logs receiver on :4317 that settles budget
// reservations from the gateway access-log stream (9.3, 13.3).
package settlement

import (
	"context"
	"log/slog"
	"strconv"

	collogs "go.opentelemetry.io/proto/otlp/collector/logs/v1"
	commonpb "go.opentelemetry.io/proto/otlp/common/v1"

	"github.com/X-McKay/zetra/platform/aps/internal/metrics"
)

// Settler replaces a reservation with actual usage.
type Settler interface {
	Settle(ctx context.Context, decision string, actual int64) (bool, error)
}

type Receiver struct {
	collogs.UnimplementedLogsServiceServer
	S       Settler
	Metrics *metrics.Metrics
	Log     *slog.Logger
}

func intOf(v *commonpb.AnyValue) (int64, bool) {
	switch x := v.GetValue().(type) {
	case *commonpb.AnyValue_IntValue:
		return x.IntValue, true
	case *commonpb.AnyValue_DoubleValue:
		return int64(x.DoubleValue), true
	case *commonpb.AnyValue_StringValue:
		n, err := strconv.ParseInt(x.StringValue, 10, 64)
		return n, err == nil
	}
	return 0, false
}

// fields collects the attributes we read from the record's attributes and,
// when the body is a key-value list, from the body.
func fields(attrs []*commonpb.KeyValue, body *commonpb.AnyValue) map[string]*commonpb.AnyValue {
	out := map[string]*commonpb.AnyValue{}
	add := func(kvs []*commonpb.KeyValue) {
		for _, kv := range kvs {
			switch kv.GetKey() {
			case "agent.decision_id", "llm.tokens_in", "llm.tokens_out":
				out[kv.GetKey()] = kv.GetValue()
			}
		}
	}
	add(body.GetKvlistValue().GetValues())
	add(attrs)
	return out
}

func (r *Receiver) Export(ctx context.Context, req *collogs.ExportLogsServiceRequest) (*collogs.ExportLogsServiceResponse, error) {
	for _, rl := range req.GetResourceLogs() {
		for _, sl := range rl.GetScopeLogs() {
			for _, rec := range sl.GetLogRecords() {
				f := fields(rec.GetAttributes(), rec.GetBody())
				decision := f["agent.decision_id"].GetStringValue()
				in, okIn := intOf(f["llm.tokens_in"])
				out, okOut := intOf(f["llm.tokens_out"])
				if decision == "" || (!okIn && !okOut) {
					continue
				}
				ok, err := r.S.Settle(ctx, decision, max(in, 0)+max(out, 0))
				switch {
				case err != nil:
					r.Metrics.Settled("error")
					if r.Log != nil {
						r.Log.Warn("settlement failed", "decision_id", decision, "error", err.Error())
					}
				case ok:
					r.Metrics.Settled("settled")
				default:
					r.Metrics.Settled("no_reservation")
				}
			}
		}
	}
	return &collogs.ExportLogsServiceResponse{}, nil
}
