// Command aps is the Agent Policy Service (spec section 9).
package main

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	authv3 "github.com/envoyproxy/go-control-plane/envoy/service/auth/v3"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"github.com/redis/go-redis/v9"
	collogs "go.opentelemetry.io/proto/otlp/collector/logs/v1"
	"google.golang.org/grpc"
	"k8s.io/client-go/dynamic"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"

	"github.com/X-McKay/zetra/platform/aps/internal/config"
	"github.com/X-McKay/zetra/platform/aps/internal/decision"
	"github.com/X-McKay/zetra/platform/aps/internal/exchange"
	"github.com/X-McKay/zetra/platform/aps/internal/httpapi"
	"github.com/X-McKay/zetra/platform/aps/internal/jwtverify"
	"github.com/X-McKay/zetra/platform/aps/internal/kube"
	"github.com/X-McKay/zetra/platform/aps/internal/metrics"
	"github.com/X-McKay/zetra/platform/aps/internal/podindex"
	"github.com/X-McKay/zetra/platform/aps/internal/registry"
	"github.com/X-McKay/zetra/platform/aps/internal/settlement"
	"github.com/X-McKay/zetra/platform/aps/internal/store"
	"github.com/X-McKay/zetra/platform/aps/internal/vault"
)

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	if err := run(log); err != nil {
		log.Error("aps exited", "error", err.Error())
		os.Exit(1)
	}
}

func run(log *slog.Logger) error {
	cfg, err := config.Load()
	if err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	rc, err := rest.InClusterConfig()
	if err != nil {
		return err
	}
	cs, err := kubernetes.NewForConfig(rc)
	if err != nil {
		return err
	}
	dyn, err := dynamic.NewForConfig(rc)
	if err != nil {
		return err
	}

	var st store.Store
	switch cfg.StoreBackend {
	case "memory":
		log.Warn("memory store: single replica only")
		st = store.NewMemory()
	case "redis":
		opts := &redis.Options{Addr: cfg.RedisAddr, Password: cfg.RedisPassword}
		if cfg.RedisTLS {
			if opts.TLSConfig, err = config.TLSConfig(cfg.RedisCAFile); err != nil {
				return err
			}
		}
		st = store.NewRedis(redis.NewClient(opts))
	default:
		return errors.New("APS_STORE must be redis or memory")
	}

	key, err := vault.LoadKey()
	if err != nil {
		return err
	}
	vlt, err := vault.New(key)
	if err != nil {
		return err
	}
	idp, err := config.HTTPClient(cfg.IdPCAFile)
	if err != nil {
		return err
	}

	reg := prometheus.NewRegistry()
	reg.MustRegister(collectors.NewGoCollector(), collectors.NewProcessCollector(collectors.ProcessCollectorOpts{}))
	m := metrics.New(reg)

	verifier := jwtverify.New(cfg.IssuerURL, cfg.JWKSURL, idp)
	k := &kube.Client{CS: cs}
	pods := podindex.New(cs)
	inf := registry.NewInformers(cs, dyn)
	rg := registry.New(inf)
	entries := &vault.Entries{Store: st, Vault: vlt, Now: time.Now}
	x := &exchange.Exchanger{
		Adapter: &exchange.Keycloak{TokenEndpoint: cfg.TokenEndpoint, ClientID: cfg.ClientID,
			ClientSecret: cfg.ClientSecret, CLIClientID: cfg.CLIClientID, HTTP: idp},
		Store: st, Vault: vlt, Entries: entries, Verifier: verifier, Kube: k,
		Issuer: cfg.IssuerURL, Metrics: m, Now: time.Now,
	}

	pods.Run(ctx.Done())
	inf.Run(ctx.Done())
	go fetchJWKS(ctx, verifier, log)
	go vaultGauge(ctx, st, m)

	// :9001 ext_authz
	gs := grpc.NewServer()
	authv3.RegisterAuthorizationServer(gs, &decision.Server{
		D: buildDeps(verifier, pods, rg, st, entries, x, cfg.Catalogue), Log: decision.NewLogger(os.Stdout), Metrics: m,
	})
	// :4317 OTLP settlement
	os2 := grpc.NewServer()
	collogs.RegisterLogsServiceServer(os2, &settlement.Receiver{S: st, Metrics: m, Log: log})

	api := &httpapi.API{
		Kube: k, Store: st, Entries: entries, Verifier: verifier, Pods: pods,
		IssuerURL: cfg.IssuerURL, TokenEndpoint: cfg.TokenEndpoint, IdP: idp,
		RunnerSubjects: cfg.RunnerSubjects, ReconcilerSubjects: cfg.ReconcilerSubjects, Log: log, Now: time.Now,
	}
	health := http.NewServeMux()
	health.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) })
	health.HandleFunc("GET /readyz", func(w http.ResponseWriter, r *http.Request) {
		pctx, cancel := context.WithTimeout(r.Context(), time.Second)
		defer cancel()
		if !pods.HasSynced() || !inf.HasSynced() || !verifier.Fetched() || st.Ping(pctx) != nil {
			w.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		w.WriteHeader(http.StatusOK)
	})
	servers := []*http.Server{
		{Addr: cfg.HTTPAddr, Handler: api.Handler(), ReadHeaderTimeout: 5 * time.Second},
		{Addr: cfg.HealthAddr, Handler: health, ReadHeaderTimeout: 5 * time.Second},
		{Addr: cfg.MetricsAddr, Handler: promhttp.HandlerFor(reg, promhttp.HandlerOpts{}), ReadHeaderTimeout: 5 * time.Second},
	}

	errc := make(chan error, 5)
	serveGRPC := func(s *grpc.Server, addr string) {
		l, err := net.Listen("tcp", addr)
		if err != nil {
			errc <- err
			return
		}
		errc <- s.Serve(l)
	}
	go serveGRPC(gs, cfg.GRPCAddr)
	go serveGRPC(os2, cfg.OTLPAddr)
	for _, s := range servers {
		go func(s *http.Server) {
			if err := s.ListenAndServe(); !errors.Is(err, http.ErrServerClosed) {
				errc <- err
			}
		}(s)
	}
	log.Info("aps started", "grpc", cfg.GRPCAddr, "http", cfg.HTTPAddr, "otlp", cfg.OTLPAddr, "store", cfg.StoreBackend)

	select {
	case <-ctx.Done():
	case err = <-errc:
	}
	sctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	for _, s := range servers {
		_ = s.Shutdown(sctx)
	}
	gs.GracefulStop()
	os2.GracefulStop()
	return err
}

func fetchJWKS(ctx context.Context, v *jwtverify.Verifier, log *slog.Logger) {
	for {
		if err := v.Refresh(ctx); err == nil {
			return
		} else {
			log.Warn("jwks fetch failed; retrying", "error", err.Error())
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(5 * time.Second):
		}
	}
}

func vaultGauge(ctx context.Context, st store.Store, m *metrics.Metrics) {
	t := time.NewTicker(30 * time.Second)
	defer t.Stop()
	for {
		if n, err := st.CountVault(ctx); err == nil {
			m.VaultEntries(n)
		}
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}
