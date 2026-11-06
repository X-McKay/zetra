// capture: the conformance capture proxy. CAPTURE_LISTEN (default :8080) and
// CAPTURE_UPSTREAM (default http://127.0.0.1:8081); one JSON record per request on stdout.
// Optional CAPTURE_BACKEND_KEY_FILE enables a boolean comparison to a mounted raw key.
package main

import (
	"errors"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"

	"github.com/X-McKay/zetra/platform/capture/internal/capture"
)

func env(name, fallback string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	return fallback
}

// A configured but unusable key must stop the proxy; otherwise T7 could silently
// lose its credential comparison. Errors never include file contents or paths.
func backendKeyFromEnv() (string, error) {
	path, configured := os.LookupEnv("CAPTURE_BACKEND_KEY_FILE")
	if !configured {
		return "", nil
	}
	invalid := errors.New("CAPTURE_BACKEND_KEY_FILE must contain one nonempty raw key")
	f, err := os.Open(path)
	if err != nil {
		return "", invalid
	}
	defer f.Close()
	raw, err := io.ReadAll(io.LimitReader(f, 16385))
	if err != nil || len(raw) > 16384 {
		return "", invalid
	}
	key := strings.TrimSpace(string(raw))
	if key == "" || strings.ContainsAny(key, " \t\r\n") {
		return "", invalid
	}
	for _, c := range key {
		if c < 33 || c > 126 {
			return "", invalid
		}
	}
	return key, nil
}

func main() {
	key, err := backendKeyFromEnv()
	if err != nil {
		log.Fatal(err)
	}
	upstream, err := url.Parse(env("CAPTURE_UPSTREAM", "http://127.0.0.1:8081"))
	if err != nil {
		log.Fatalf("CAPTURE_UPSTREAM: %v", err)
	}
	srv := &http.Server{
		Addr:              env("CAPTURE_LISTEN", ":8080"),
		Handler:           capture.HandlerWithBackendKey(upstream, os.Stdout, key),
		ReadHeaderTimeout: 10 * time.Second,
	}
	log.Printf("capture proxy %s -> %s", srv.Addr, upstream)
	log.Fatal(srv.ListenAndServe())
}
