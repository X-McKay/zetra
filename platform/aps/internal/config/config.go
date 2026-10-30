// Package config reads the APS environment (9.1).
package config

import (
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"strings"
	"time"
)

type Config struct {
	IssuerURL     string
	TokenEndpoint string
	JWKSURL       string
	Adapter       string
	ClientID      string
	ClientSecret  string
	CLIClientID   string

	StoreBackend  string // redis | memory
	RedisAddr     string
	RedisPassword string
	RedisTLS      bool
	RedisCAFile   string

	IdPCAFile string
	// Backend name -> reserve_tokens (the backend's maxModelLen, 3.7).
	Catalogue map[string]int

	RunnerSubjects     []string
	ReconcilerSubjects []string

	GRPCAddr, HTTPAddr, OTLPAddr, MetricsAddr, HealthAddr string
}

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

// secret reads K, or the file named by K_FILE.
func secret(k string) (string, error) {
	if v := os.Getenv(k); v != "" {
		return v, nil
	}
	if f := os.Getenv(k + "_FILE"); f != "" {
		b, err := os.ReadFile(f)
		return strings.TrimSpace(string(b)), err
	}
	return "", nil
}

func list(k, def string) []string {
	var out []string
	for _, s := range strings.Split(env(k, def), ",") {
		if s = strings.TrimSpace(s); s != "" {
			out = append(out, s)
		}
	}
	return out
}

func Load() (*Config, error) {
	c := &Config{
		IssuerURL:          os.Getenv("ISSUER_URL"),
		Adapter:            env("IDP_ADAPTER", "keycloak"),
		ClientID:           env("IDP_CLIENT_ID", "agent-policy-service"),
		CLIClientID:        env("IDP_CLI_CLIENT_ID", "agent-cli"),
		StoreBackend:       env("APS_STORE", "redis"),
		RedisAddr:          env("REDIS_ADDR", "redis.agent-platform.svc.cluster.local:6379"),
		RedisTLS:           env("REDIS_TLS", "true") == "true",
		RedisCAFile:        os.Getenv("REDIS_CA_FILE"),
		IdPCAFile:          os.Getenv("IDP_CA_FILE"),
		RunnerSubjects:     list("APS_RUNNER_SUBJECTS", "system:serviceaccount:agent-platform:session-runner"),
		ReconcilerSubjects: list("APS_RECONCILER_SUBJECTS", "system:serviceaccount:agent-platform:cleanup-reconciler"),
		GRPCAddr:           env("APS_GRPC_ADDR", ":9001"),
		HTTPAddr:           env("APS_HTTP_ADDR", ":9002"),
		OTLPAddr:           env("APS_OTLP_ADDR", ":4317"),
		MetricsAddr:        env("APS_METRICS_ADDR", ":9090"),
		HealthAddr:         env("APS_HEALTH_ADDR", ":8081"),
	}
	if c.IssuerURL == "" {
		return nil, errors.New("ISSUER_URL is required")
	}
	c.TokenEndpoint = env("IDP_TOKEN_ENDPOINT", c.IssuerURL+"/protocol/openid-connect/token")
	c.JWKSURL = env("IDP_JWKS_URL", c.IssuerURL+"/protocol/openid-connect/certs")
	var err error
	if c.ClientSecret, err = secret("IDP_CLIENT_SECRET"); err != nil {
		return nil, err
	}
	if c.RedisPassword, err = secret("REDIS_PASSWORD"); err != nil {
		return nil, err
	}
	cat, err := secret("APS_BACKEND_CATALOGUE")
	if err != nil {
		return nil, err
	}
	if c.Catalogue, err = ParseCatalogue(cat); err != nil {
		return nil, err
	}
	if c.Adapter != "keycloak" {
		return nil, fmt.Errorf("IDP_ADAPTER %q is not implemented", c.Adapter)
	}
	return c, nil
}

// ParseCatalogue reads {"<backend>": {"maxModelLen": N}, ...}.
func ParseCatalogue(s string) (map[string]int, error) {
	out := map[string]int{}
	if strings.TrimSpace(s) == "" {
		return out, nil
	}
	var raw map[string]struct {
		MaxModelLen int `json:"maxModelLen"`
	}
	if err := json.Unmarshal([]byte(s), &raw); err != nil {
		return nil, fmt.Errorf("APS_BACKEND_CATALOGUE: %w", err)
	}
	for k, v := range raw {
		if v.MaxModelLen <= 0 {
			return nil, fmt.Errorf("APS_BACKEND_CATALOGUE: %s has no maxModelLen", k)
		}
		out[k] = v.MaxModelLen
	}
	return out, nil
}

// TLSConfig returns a client TLS config trusting caFile in addition to system roots.
func TLSConfig(caFile string) (*tls.Config, error) {
	cfg := &tls.Config{MinVersion: tls.VersionTLS12}
	if caFile == "" {
		return cfg, nil
	}
	pool, err := x509.SystemCertPool()
	if err != nil {
		pool = x509.NewCertPool()
	}
	pem, err := os.ReadFile(caFile)
	if err != nil {
		return nil, err
	}
	if !pool.AppendCertsFromPEM(pem) {
		return nil, fmt.Errorf("no certificates in %s", caFile)
	}
	cfg.RootCAs = pool
	return cfg, nil
}

// HTTPClient builds the IdP client.
func HTTPClient(caFile string) (*http.Client, error) {
	tc, err := TLSConfig(caFile)
	if err != nil {
		return nil, err
	}
	tr := http.DefaultTransport.(*http.Transport).Clone()
	tr.TLSClientConfig = tc
	return &http.Client{Transport: tr, Timeout: 6 * time.Second}, nil
}
