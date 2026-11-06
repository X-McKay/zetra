package main

import (
	"errors"
	"github.com/X-McKay/zetra/platform/budget-backend/internal/backend"
	"io"
	"log/slog"
	"net/http"
	"os"
	"slices"
	"strings"
	"time"
)

func config() (string, []string, error) {
	file, err := os.Open(os.Getenv("BUDGET_BACKEND_KEY_FILE"))
	if err != nil {
		return "", nil, errors.New("backend key file unavailable")
	}
	defer file.Close()
	raw, err := io.ReadAll(io.LimitReader(file, 16385))
	key := strings.TrimSpace(string(raw))
	if err != nil || len(raw) > 16384 || key == "" || strings.ContainsAny(key, " \t\r\n") {
		return "", nil, errors.New("invalid backend key")
	}
	for _, c := range []byte(key) {
		if c < 33 || c > 126 {
			return "", nil, errors.New("invalid backend key")
		}
	}
	agents := strings.Split(os.Getenv("BUDGET_BACKEND_AGENTS"), ",")
	for _, agent := range agents {
		if !slices.Contains([]string{"tenant-test/conformance-budget-day", "tenant-test/conformance-budget-day-retry", "tenant-test/conformance-budget-session"}, agent) {
			return "", nil, errors.New("explicit budget fixture allowlist required")
		}
	}
	return key, agents, nil
}
func main() {
	log := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	key, agents, err := config()
	if err != nil {
		log.Error("configuration refused", "error", err.Error())
		os.Exit(1)
	}
	addr := os.Getenv("BUDGET_BACKEND_ADDR")
	if addr == "" {
		addr = ":8080"
	}
	srv := &http.Server{Addr: addr, Handler: backend.New(key, agents, log), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 5 * time.Second, WriteTimeout: 20 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 8192}
	if err = srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Error("server stopped", "error_class", "listen")
		os.Exit(1)
	}
}
