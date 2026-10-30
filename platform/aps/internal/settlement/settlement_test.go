package settlement

import (
	"context"
	"testing"

	collogs "go.opentelemetry.io/proto/otlp/collector/logs/v1"
	commonpb "go.opentelemetry.io/proto/otlp/common/v1"
	logspb "go.opentelemetry.io/proto/otlp/logs/v1"
)

type rec map[string]int64

func (r rec) Settle(_ context.Context, d string, n int64) (bool, error) { r[d] = n; return true, nil }

func kv(k string, v *commonpb.AnyValue) *commonpb.KeyValue {
	return &commonpb.KeyValue{Key: k, Value: v}
}
func s(v string) *commonpb.AnyValue {
	return &commonpb.AnyValue{Value: &commonpb.AnyValue_StringValue{StringValue: v}}
}
func i(v int64) *commonpb.AnyValue {
	return &commonpb.AnyValue{Value: &commonpb.AnyValue_IntValue{IntValue: v}}
}

func TestSettlesTokensInPlusOut(t *testing.T) {
	r := rec{}
	recv := &Receiver{S: r}
	req := &collogs.ExportLogsServiceRequest{ResourceLogs: []*logspb.ResourceLogs{{ScopeLogs: []*logspb.ScopeLogs{{LogRecords: []*logspb.LogRecord{
		{Attributes: []*commonpb.KeyValue{kv("agent.decision_id", s("d1")), kv("llm.tokens_in", i(100)), kv("llm.tokens_out", i(23))}},
		{Body: &commonpb.AnyValue{Value: &commonpb.AnyValue_KvlistValue{KvlistValue: &commonpb.KeyValueList{Values: []*commonpb.KeyValue{
			kv("agent.decision_id", s("d2")), kv("llm.tokens_in", s("7")), kv("llm.tokens_out", s("3"))}}}}},
		{Attributes: []*commonpb.KeyValue{kv("agent.decision_id", s("d3"))}}, // MCP record: no usage, not settled
		{Attributes: []*commonpb.KeyValue{kv("llm.tokens_in", i(5))}},
	}}}}}}
	if _, err := recv.Export(context.Background(), req); err != nil {
		t.Fatal(err)
	}
	if r["d1"] != 123 || r["d2"] != 10 || len(r) != 2 {
		t.Fatalf("settled %v", r)
	}
}
