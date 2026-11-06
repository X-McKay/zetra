package config

import "testing"

func TestLifecycleObserverConfig(t *testing.T) {
	t.Setenv("ISSUER_URL", "https://issuer.example")
	for _, tc := range []struct {
		subject, agents string
		fail            bool
	}{
		{"", "", false}, {"system:serviceaccount:agent-platform:conformance-harness", "conformance-lifecycle", false},
		{"alice", "conformance-lifecycle", true}, {"system:serviceaccount:tenant-test:agent-conformance", "conformance-lifecycle", true},
		{"system:serviceaccount:agent-platform:conformance-harness", "", true}, {"", "conformance-lifecycle", true},
		{"system:serviceaccount:agent-platform:conformance-harness", "conformance", true},
	} {
		t.Setenv("APS_LIFECYCLE_OBSERVER_SUBJECT", tc.subject)
		t.Setenv("APS_LIFECYCLE_OBSERVER_AGENTS", tc.agents)
		_, err := Load()
		if (err != nil) != tc.fail {
			t.Fatalf("subject %q agents %q error %v", tc.subject, tc.agents, err)
		}
	}
}
