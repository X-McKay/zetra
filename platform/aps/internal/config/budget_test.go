package config

import "testing"

func TestBudgetObserverConfig(t *testing.T) {
	t.Setenv("ISSUER_URL", "https://issuer.example")
	for _, tc := range []struct {
		subject, agents string
		fail            bool
	}{
		{"", "", false}, {"system:serviceaccount:agent-platform:conformance-harness", "conformance-budget-day,conformance-budget-session", false},
		{"system:serviceaccount:agent-platform:conformance-harness", "conformance-budget-day-retry", false},
		{"system:serviceaccount:agent-platform:conformance-harness", "conformance-budget-day-arbitrary", true},
		{"alice", "conformance-budget-day", true}, {"system:serviceaccount:tenant-test:agent-conformance", "conformance-budget-day", true},
		{"system:serviceaccount:agent-platform:conformance-harness", "", true}, {"", "conformance-budget-day", true},
		{"system:serviceaccount:agent-platform:conformance-harness", "conformance", true},
	} {
		t.Setenv("APS_BUDGET_OBSERVER_SUBJECT", tc.subject)
		t.Setenv("APS_BUDGET_OBSERVER_AGENTS", tc.agents)
		_, err := Load()
		if (err != nil) != tc.fail {
			t.Fatalf("subject %q agents %q error %v", tc.subject, tc.agents, err)
		}
	}
}
