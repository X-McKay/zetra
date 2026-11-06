package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestBackendKeyConfigIsOptionalButNeverSilentlyIgnored(t *testing.T) {
	t.Setenv("CAPTURE_BACKEND_KEY_FILE", "")
	if _, err := backendKeyFromEnv(); err == nil {
		t.Fatal("configured empty path was ignored")
	}
	if err := os.Unsetenv("CAPTURE_BACKEND_KEY_FILE"); err != nil {
		t.Fatal(err)
	}
	if key, err := backendKeyFromEnv(); err != nil || key != "" {
		t.Fatal("MCP mode must remain available")
	}
	path := filepath.Join(t.TempDir(), "key")
	t.Setenv("CAPTURE_BACKEND_KEY_FILE", path)
	if _, err := backendKeyFromEnv(); err == nil {
		t.Fatal("missing key file was ignored")
	}
	for _, content := range []string{"", " \n", "Bearer wrong", "line1\nline2", strings.Repeat("x", 16385)} {
		if err := os.WriteFile(path, []byte(content), 0600); err != nil {
			t.Fatal(err)
		}
		if _, err := backendKeyFromEnv(); err == nil {
			t.Fatal("invalid key file was accepted")
		}
	}
	if err := os.WriteFile(path, []byte("test-engine-key\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if key, err := backendKeyFromEnv(); err != nil || key != "test-engine-key" {
		t.Fatal("valid key file failed")
	}
}
