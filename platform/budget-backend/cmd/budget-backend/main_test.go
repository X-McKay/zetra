package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestConfiguredKeyAndFixtureAllowlistFailClosed(t *testing.T) {
	path := filepath.Join(t.TempDir(), "key")
	t.Setenv("BUDGET_BACKEND_KEY_FILE", path)
	t.Setenv("BUDGET_BACKEND_AGENTS", "tenant-test/conformance-budget-day")
	if _, _, err := config(); err == nil {
		t.Fatal("missing configured key accepted")
	}
	for _, value := range []string{"", "a b", "a\x00b", "é"} {
		if err := os.WriteFile(path, []byte(value), 0600); err != nil {
			t.Fatal(err)
		}
		if _, _, err := config(); err == nil {
			t.Fatal("invalid key accepted")
		}
	}
	_ = os.WriteFile(path, []byte("valid-key\n"), 0600)
	if key, agents, err := config(); err != nil || key != "valid-key" || len(agents) != 1 {
		t.Fatal("valid config refused")
	}
	t.Setenv("BUDGET_BACKEND_AGENTS", "tenant-a/other")
	if _, _, err := config(); err == nil {
		t.Fatal("non-fixture identity accepted")
	}
}
