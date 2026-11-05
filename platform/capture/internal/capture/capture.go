// Package capture is the conformance capture proxy (spec 15.2 T7, T8, T10, T11, T12):
// a sidecar in front of a backend that records, for every request it forwards, what the
// backend received: the method and path, every x-agent-* header, and the claims of the
// bearer token that identify it (sub, act.sub, aud, azp, jti, exp). It never logs the token,
// any other header value, or a body (13.6); a captured token is compared by jti (15.4).
package capture

import (
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strings"
	"sync"
	"time"
)

// Record is one forwarded request as the backend saw it.
type Record struct {
	TS         string            `json:"ts"`
	Method     string            `json:"method"`
	Host       string            `json:"host"` // the Host header as it arrived
	Path       string            `json:"path"`
	Status     int               `json:"status"`
	DurationMS int64             `json:"ms"`
	Agent      map[string]string `json:"x_agent"` // every x-agent-* header, lower-cased names
	Bearer     *Bearer           `json:"bearer"`  // nil when the request carried no bearer
}

// Bearer is the identifying subset of a JWT's claims; never the token.
type Bearer struct {
	Sub    string   `json:"sub,omitempty"`
	ActSub string   `json:"act_sub,omitempty"`
	Aud    []string `json:"aud,omitempty"`
	Azp    string   `json:"azp,omitempty"`
	JTI    string   `json:"jti,omitempty"`
	Exp    int64    `json:"exp,omitempty"`
	Error  string   `json:"error,omitempty"` // "not_a_jwt" when the bearer is not a JWT
}

// Claims decodes the payload of a compact JWT without verifying it: the proxy only reports
// what arrived; the backend is what verifies. It never returns the token.
func Claims(token string) *Bearer {
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return &Bearer{Error: "not_a_jwt"}
	}
	raw, err := base64.RawURLEncoding.DecodeString(strings.TrimRight(parts[1], "="))
	if err != nil {
		return &Bearer{Error: "not_a_jwt"}
	}
	var c struct {
		Sub string               `json:"sub"`
		Act struct{ Sub string } `json:"act"`
		Aud json.RawMessage      `json:"aud"`
		Azp string               `json:"azp"`
		JTI string               `json:"jti"`
		Exp int64                `json:"exp"`
	}
	if json.Unmarshal(raw, &c) != nil {
		return &Bearer{Error: "not_a_jwt"}
	}
	b := &Bearer{Sub: c.Sub, ActSub: c.Act.Sub, Azp: c.Azp, JTI: c.JTI, Exp: c.Exp}
	var one string
	if json.Unmarshal(c.Aud, &one) == nil {
		b.Aud = []string{one}
	} else {
		_ = json.Unmarshal(c.Aud, &b.Aud)
	}
	return b
}

// Observe builds the record of a request before it is forwarded; the status and duration
// are filled in afterwards.
func Observe(r *http.Request) Record {
	rec := Record{
		TS:     time.Now().UTC().Format("2006-01-02T15:04:05.000Z"),
		Method: r.Method,
		Host:   r.Host,
		Path:   r.URL.Path,
		Agent:  map[string]string{},
	}
	for name, values := range r.Header {
		lower := strings.ToLower(name)
		if strings.HasPrefix(lower, "x-agent-") && len(values) > 0 {
			rec.Agent[lower] = values[0]
		}
	}
	if auth := r.Header.Get("Authorization"); auth != "" {
		if strings.HasPrefix(strings.ToLower(auth), "bearer ") {
			rec.Bearer = Claims(strings.TrimSpace(auth[7:]))
		} else {
			rec.Bearer = &Bearer{Error: "not_bearer"}
		}
	}
	return rec
}

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (w *statusWriter) WriteHeader(code int) { w.status = code; w.ResponseWriter.WriteHeader(code) }

func (w *statusWriter) Flush() {
	if f, ok := w.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

// Handler forwards every request to upstream and writes one JSON record per request to out.
// Headers and body pass through untouched except Host: the upstream is reached on loopback,
// and an MCP server built on the Go SDK rejects a loopback request whose Host is not
// localhost (DNS rebinding protection, 403), so Host becomes the upstream address and the
// original travels as X-Forwarded-Host and in the record. FlushInterval -1 keeps streamed
// (SSE) responses flowing.
func Handler(upstream *url.URL, out io.Writer) http.Handler {
	proxy := &httputil.ReverseProxy{
		Rewrite: func(r *httputil.ProxyRequest) {
			r.SetURL(upstream)
			r.SetXForwarded()
			r.Out.Host = upstream.Host
		},
		FlushInterval: -1,
	}
	var mu sync.Mutex
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rec := Observe(r)
		start := time.Now()
		sw := &statusWriter{ResponseWriter: w, status: http.StatusOK}
		proxy.ServeHTTP(sw, r)
		rec.Status = sw.status
		rec.DurationMS = time.Since(start).Milliseconds()
		line, err := json.Marshal(rec)
		if err != nil {
			return
		}
		mu.Lock()
		_, _ = out.Write(append(line, '\n'))
		mu.Unlock()
	})
}
