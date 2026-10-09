#!/usr/bin/env bash
# Local developer fixture only: deliberately enables OpenShell's lab auth flag.
set -euo pipefail

zetra_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$zetra_root"
zetra_kubeconfig="$zetra_root/.tools/zetra-kubeconfig"
zetra_cache="$zetra_root/.tools/local-platform-cache"
zetra_python="$zetra_root/.venv/bin/python"
zetra_context=kind-zetra-validation
zetra_actions=()
test "$#" -eq 0 || { echo 'This script accepts no alternate targets or flags.' >&2; exit 2; }
test "$(uname -s)" = Darwin && test "$(uname -m)" = arm64 || \
  { echo 'This pinned lab bootstrap supports macOS ARM64 only; no other CLI architecture is qualified.' >&2; exit 2; }
test -f "$zetra_kubeconfig" || { echo 'Create the dedicated disposable cluster first.' >&2; exit 2; }
test -x "$zetra_python" || { echo 'Bootstrap the locked project Python environment first.' >&2; exit 2; }
for zetra_tool in kubectl helm curl shasum tar; do
  command -v "$zetra_tool" >/dev/null || { echo "Missing prerequisite: $zetra_tool" >&2; exit 2; }
done
zetra_kubectl=(kubectl --kubeconfig "$zetra_kubeconfig" --context "$zetra_context" --request-timeout=15s)
zetra_helm=(helm --kubeconfig "$zetra_kubeconfig" --kube-context "$zetra_context")

# Refuse non-loopback APIs, unexpected nodes, and multiple-node installations.
# No KUBECONFIG/default-context mutation, alternate-target argument, or repo add.
zetra_server="$("${zetra_kubectl[@]}" config view --minify -o jsonpath='{.clusters[0].cluster.server}')"
"$zetra_python" -c 'import sys,urllib.parse; u=urllib.parse.urlsplit(sys.argv[1]); assert u.scheme=="https" and u.hostname in {"127.0.0.1","localhost","::1"}, "Disposable loopback API required"' "$zetra_server"
zetra_nodes="$("${zetra_kubectl[@]}" get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')"
test "$zetra_nodes" = zetra-validation-control-plane || { echo 'Expected only the disposable kind node; refusing writes.' >&2; exit 2; }

mkdir -p "$zetra_cache"
export HELM_CACHE_HOME="$zetra_root/.tools/helm/cache"
export HELM_CONFIG_HOME="$zetra_root/.tools/helm/config"
export HELM_DATA_HOME="$zetra_root/.tools/helm/data"
mkdir -p "$HELM_CACHE_HOME" "$HELM_CONFIG_HOME" "$HELM_DATA_HOME"

verify_sha() {
  local zetra_file="$1" zetra_expected="$2" zetra_actual
  zetra_actual="$(shasum -a 256 "$zetra_file")"
  test "${zetra_actual%% *}" = "$zetra_expected" || { echo "Checksum mismatch: $zetra_file" >&2; exit 2; }
}
download_verified() {
  local zetra_url="$1" zetra_file="$2" zetra_sha="$3"
  if ! test -f "$zetra_file"; then
    curl --proto '=https' --tlsv1.2 -fsSL "$zetra_url" -o "$zetra_file.tmp"
    verify_sha "$zetra_file.tmp" "$zetra_sha"
    mv "$zetra_file.tmp" "$zetra_file"
  fi
  verify_sha "$zetra_file" "$zetra_sha"
}

download_verified https://helm.cilium.io/cilium-1.20.2.tgz \
  "$zetra_cache/cilium-1.20.2.tgz" b2afd87b7f75f875f92a14559f14f59b7babbb479d968e3fd625a20bf30ec20e
download_verified https://helm.cilium.io/tetragon-1.7.1.tgz \
  "$zetra_cache/tetragon-1.7.1.tgz" 79ec0f7d35b6fcad99ca317348deac136c00b674430c28be17aa818d2015c632
download_verified https://github.com/kubernetes-sigs/agent-sandbox/releases/download/v1.0.6/sandbox.yaml \
  "$zetra_cache/agent-sandbox-1.0.6.yaml" b995db5839f2bdca3a24b8bbefdec405df662d6dd223eec8f5dc74098c974231
download_verified https://github.com/NVIDIA/OpenShell/releases/download/v0.1.2/openshell-aarch64-apple-darwin.tar.gz \
  "$zetra_cache/openshell-aarch64-apple-darwin.tar.gz" cdde7e92bd7eac664031cf171cfe80d29e7f122a6674917b25a4ce0bcbc33466
if ! test -f "$zetra_root/.tools/openshell"; then
  tar -xzf "$zetra_cache/openshell-aarch64-apple-darwin.tar.gz" -C "$zetra_cache" openshell
  verify_sha "$zetra_cache/openshell" 789093ba9278271f2617642cadfabe58d3c08f9f8a5a11c29dcbd4ab60d6611f
  cp "$zetra_cache/openshell" "$zetra_root/.tools/openshell"
  chmod +x "$zetra_root/.tools/openshell"
fi
verify_sha "$zetra_root/.tools/openshell" 789093ba9278271f2617642cadfabe58d3c08f9f8a5a11c29dcbd4ab60d6611f
test "$("$zetra_root/.tools/openshell" --version)" = 'openshell 0.1.2'
if ! test -f "$zetra_cache/helm-chart-0.1.2.tgz"; then
  "${zetra_helm[@]}" pull oci://ghcr.io/nvidia/openshell/helm-chart \
    --version 0.1.2 --destination "$zetra_cache"
fi
verify_sha "$zetra_cache/helm-chart-0.1.2.tgz" b9250541d47c5ce4cfcfeb6a652ddcbd1f444a33e151ff43b16daec5d013ff04

# Exact installed releases skip writes. Drift requires a reviewed local repair;
# this bootstrap never silently upgrades or overwrites an unexpected tuple.
ensure_release() {
  local zetra_release="$1" zetra_namespace="$2" zetra_chart="$3" zetra_archive="$4"
  shift 4
  local zetra_installed
  zetra_installed="$("${zetra_helm[@]}" list -n "$zetra_namespace" --all -o json | \
    "$zetra_python" -c 'import json,sys; rows=[r for r in json.load(sys.stdin) if r["name"]==sys.argv[1]]; print((rows[0]["chart"]+":"+rows[0]["status"]) if rows else "")' "$zetra_release")"
  if test -n "$zetra_installed"; then
    test "$zetra_installed" = "$zetra_chart:deployed" || { echo "Unexpected release $zetra_release: $zetra_installed; refusing upgrade." >&2; exit 2; }
    echo "SKIP installed $zetra_release $zetra_chart"
    zetra_actions+=("$zetra_release:skipped")
  else
    "${zetra_helm[@]}" install "$zetra_release" "$zetra_archive" \
      -n "$zetra_namespace" --create-namespace --wait --timeout 3m "$@"
    zetra_actions+=("$zetra_release:installed")
  fi
}
assert_values() {
  local zetra_release="$1" zetra_namespace="$2" zetra_checks="$3"
  "${zetra_helm[@]}" get values "$zetra_release" -n "$zetra_namespace" --all -o json | \
    "$zetra_python" -c 'import json,sys; v=json.load(sys.stdin)
for key,want in json.loads(sys.argv[1]).items():
 got=v
 for part in key.split("."): got=got[part]
 assert got==want, f"Installed value drift: {key}"
' "$zetra_checks"
}

# Fresh checked-in kind config has no default CNI. For the original local tuple,
# remove only its kindnet daemonset before installing Cilium; never other CNIs.
if "${zetra_kubectl[@]}" -n kube-system get daemonset kindnet --ignore-not-found -o name | "$zetra_python" -c 'import sys; sys.exit(0 if sys.stdin.read().strip() else 1)'; then
  "${zetra_kubectl[@]}" -n kube-system delete daemonset kindnet
fi
ensure_release cilium kube-system cilium-1.20.2 "$zetra_cache/cilium-1.20.2.tgz" \
  --set ipam.mode=kubernetes --set kubeProxyReplacement=false \
  --set operator.replicas=1 --set bpf.hostLegacyRouting=true --set image.pullPolicy=IfNotPresent
assert_values cilium kube-system '{"ipam.mode":"kubernetes","kubeProxyReplacement":false,"operator.replicas":1,"bpf.hostLegacyRouting":true}'
"${zetra_kubectl[@]}" -n kube-system rollout status daemonset/cilium --timeout=120s
"${zetra_kubectl[@]}" -n kube-system rollout status deployment/cilium-operator --timeout=120s
"${zetra_kubectl[@]}" wait --for=condition=Ready node/zetra-validation-control-plane --timeout=120s

ensure_release tetragon kube-system tetragon-1.7.1 "$zetra_cache/tetragon-1.7.1.tgz" \
  --set tetragon.enablePolicyFilter=true
assert_values tetragon kube-system '{"tetragon.enablePolicyFilter":true}'
"${zetra_kubectl[@]}" -n kube-system rollout status daemonset/tetragon --timeout=120s
"${zetra_kubectl[@]}" -n kube-system rollout status deployment/tetragon-operator --timeout=120s
"${zetra_kubectl[@]}" wait --for=condition=Established \
  crd/tracingpolicies.cilium.io crd/tracingpoliciesnamespaced.cilium.io --timeout=60s

zetra_controller_image="$("${zetra_kubectl[@]}" -n agent-sandbox-system get deployment agent-sandbox-controller \
  --ignore-not-found -o jsonpath='{.spec.template.spec.containers[0].image}')"
if test -z "$zetra_controller_image"; then
  "${zetra_kubectl[@]}" apply -f "$zetra_cache/agent-sandbox-1.0.6.yaml"
  zetra_actions+=("agent-sandbox:installed")
else
  test "$zetra_controller_image" = registry.k8s.io/agent-sandbox/agent-sandbox-controller:v1.0.6 || \
    { echo 'Unexpected Agent Sandbox controller version; refusing replacement.' >&2; exit 2; }
  echo 'SKIP installed Agent Sandbox v1.0.6'
  zetra_actions+=("agent-sandbox:skipped")
fi
"${zetra_kubectl[@]}" wait --for=condition=Established crd/sandboxes.agents.x-k8s.io --timeout=60s
"${zetra_kubectl[@]}" -n agent-sandbox-system rollout status deployment/agent-sandbox-controller --timeout=120s

# Keep live API preflight enabled. These auth/AppArmor/userns settings are lab
# accommodations, not production recommendations; TLS and mTLS remain enabled.
ensure_release openshell zetra-openshell helm-chart-0.1.2 "$zetra_cache/helm-chart-0.1.2.tgz" \
  --set agentSandbox.preflight.enabled=true --set server.tls.enableMtls=true \
  --set server.auth.allowUnauthenticatedUsers=true --set server.enableUserNamespaces=false \
  --set server.appArmorProfile=Unconfined --set server.telemetryEnabled=false \
  --set supervisor.sideloadMethod=init-container
assert_values openshell zetra-openshell '{"agentSandbox.preflight.enabled":true,"server.tls.enableMtls":true,"server.auth.allowUnauthenticatedUsers":true,"server.enableUserNamespaces":false,"server.appArmorProfile":"Unconfined","server.telemetryEnabled":false,"supervisor.sideloadMethod":"init-container"}'
"${zetra_kubectl[@]}" -n zetra-openshell rollout status statefulset/openshell --timeout=120s

mkdir -p "$zetra_root/integrations/evidence"
"$zetra_python" - "$zetra_root" "${zetra_actions[@]}" <<'PY'
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

root = Path(sys.argv[1])
cache = root / ".tools/local-platform-cache"
script = root / "integrations/bootstrap-local-platform.sh"
report = {
    "schemaVersion": "zetra.dev/local-platform-bootstrap-v1alpha1",
    "status": "passed",
    "createdAt": datetime.now(timezone.utc).isoformat(),
    "context": "kind-zetra-validation",
    "node": "zetra-validation-control-plane",
    "scope": "disposable-local-platform-bootstrap-readiness",
    "fixtureOnly": True,
    "scriptSha256": hashlib.sha256(script.read_bytes()).hexdigest(),
    "releaseActions": dict(item.split(":", 1) for item in sys.argv[2:]),
    "versions": {"cilium": "1.20.2", "tetragon": "1.7.1", "agentSandbox": "1.0.6", "openshell": "0.1.2"},
    "artifactHashes": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in cache.iterdir() if p.is_file() and not p.name.endswith(".tmp")},
    "cliSha256": hashlib.sha256((root / ".tools/openshell").read_bytes()).hexdigest(),
    "checks": ["explicit-loopback-local-target", "artifact-checksums", "pinned-release-and-required-values", "cilium-ready", "tetragon-ready-and-crds-established", "agent-sandbox-ready-and-crd-established", "openshell-ready-and-preflight-enabled"],
    "localAccommodations": {"allowUnauthenticatedUsers": True, "enableUserNamespaces": False, "appArmorProfile": "Unconfined", "enableMtls": True, "telemetryEnabled": False},
    "limitations": ["Readiness is not boundary-enforcement qualification", "No production principal authentication or AppArmor/user namespace qualification", "Fresh installation into a second empty cluster has not been tested"],
}
(root / "integrations/evidence/platform-bootstrap.json").write_text(json.dumps(report, indent=2) + "\n")
PY
echo 'READY pinned disposable local platform; run boundary fixtures separately.'
echo 'No production installation, principal-authentication, or enforcement attestation is implied.'
