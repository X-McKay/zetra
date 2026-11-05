// capture: the conformance capture proxy. CAPTURE_LISTEN (default :8080) and
// CAPTURE_UPSTREAM (default http://127.0.0.1:8081); one JSON record per request on stdout.
package main

import (
	"log"
	"net/http"
	"net/url"
	"os"
	"time"

	"github.com/X-McKay/zetra/platform/capture/internal/capture"
)

func env(name, fallback string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	return fallback
}

func main() {
	upstream, err := url.Parse(env("CAPTURE_UPSTREAM", "http://127.0.0.1:8081"))
	if err != nil {
		log.Fatalf("CAPTURE_UPSTREAM: %v", err)
	}
	srv := &http.Server{
		Addr:              env("CAPTURE_LISTEN", ":8080"),
		Handler:           capture.Handler(upstream, os.Stdout),
		ReadHeaderTimeout: 10 * time.Second,
	}
	log.Printf("capture proxy %s -> %s", srv.Addr, upstream)
	log.Fatal(srv.ListenAndServe())
}
