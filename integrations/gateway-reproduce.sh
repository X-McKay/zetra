#!/usr/bin/env bash
# Disposable local kind only. Requires the repository Python temporal extra.
set -euo pipefail
zetra_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$zetra_root"
zetra_kubeconfig="$zetra_root/.tools/zetra-kubeconfig"
zetra_output="$zetra_root/integrations/gateway-live-output"
test "$(uname -s)" = Darwin && test "$(uname -m)" = arm64
test -f "$zetra_kubeconfig"
mkdir -p .tools "$zetra_output"
if ! test -f .tools/agentgateway; then
  curl -fsSL https://github.com/agentgateway/agentgateway/releases/download/v1.6.0/agentgateway-darwin-arm64 -o .tools/agentgateway
fi
.venv/bin/python -c 'from pathlib import Path; import hashlib; assert hashlib.sha256(Path(".tools/agentgateway").read_bytes()).hexdigest() == "6cebe8bd57262edce23a65a376d2f5a27b97cdfef609740642edbe75e1e9c685"'
chmod +x .tools/agentgateway
zetra_kubectl=(kubectl --kubeconfig "$zetra_kubeconfig" --context kind-zetra-validation)
"${zetra_kubectl[@]}" apply -f integrations/telemetry-collector.yaml
"${zetra_kubectl[@]}" -n zetra-gateway-test rollout status deployment/otel-collector --timeout=60s
"${zetra_kubectl[@]}" -n zetra-gateway-test port-forward service/otel-collector 14317:4317 --address 127.0.0.1 > "$zetra_output/port-forward.log" 2>&1 &
zetra_forward_pid=$!
trap 'kill "$zetra_forward_pid" 2>/dev/null || true' EXIT
.venv/bin/python -c 'import socket,time; end=time.monotonic()+10
while time.monotonic()<end:
 try:
  socket.create_connection(("127.0.0.1",14317),timeout=.2).close(); break
 except OSError: time.sleep(.1)
else: raise SystemExit("Collector port-forward did not start")'
PYTHONPATH=src .venv/bin/python integrations/live-gateway-smoke.py --collector 127.0.0.1:14317 --temporal
"${zetra_kubectl[@]}" -n zetra-gateway-test logs deployment/otel-collector --timestamps > "$zetra_output/collector.log"
"${zetra_kubectl[@]}" -n zetra-gateway-test get pods -o json > "$zetra_output/collector-pods.json"
.venv/bin/python integrations/telemetry-verify.py
