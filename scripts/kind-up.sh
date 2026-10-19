#!/usr/bin/env bash
# Dedicated disposable cluster only. Run from the Zetra repository root.
set -euo pipefail
state_dir="${BRIDGE_STATE_DIR:-$PWD/.state}"
cluster_name="zetra-validation"
mkdir -p "$state_dir/tls"
chmod 700 "$state_dir" "$state_dir/tls"
kubeconfig="$state_dir/kubeconfig"
for executable in kind kubectl helm docker curl python3 rg; do command -v "$executable" >/dev/null; done
if ! kind get clusters | rg -qx "$cluster_name"; then
  kind create cluster --name "$cluster_name" --config deploy/kind/cluster.yaml --kubeconfig "$kubeconfig"
fi
# An existing cluster must have this script's explicit kubeconfig.
test -f "$kubeconfig"
kubectl --kubeconfig "$kubeconfig" config current-context | rg -qx 'kind-zetra-validation'
helm upgrade --install cilium oci://quay.io/cilium/charts/cilium --version 1.20.3 \
  --namespace kube-system --kubeconfig "$kubeconfig" --set ipam.mode=kubernetes \
  --set operator.replicas=1 --set image.pullPolicy=IfNotPresent --wait --timeout 5m
kubectl --kubeconfig "$kubeconfig" -n kube-system rollout status daemonset/cilium --timeout=300s
curl -fsSL https://github.com/kubernetes-sigs/agent-sandbox/releases/download/v1.0.7/sandbox.yaml -o "$state_dir/agent-sandbox.yaml"
curl -fsSL https://github.com/temporalio/helm-charts/releases/download/temporal-1.7.0/temporal-1.7.0.tgz -o "$state_dir/temporal.tgz"
python3 - "$state_dir" <<'PY'
import hashlib,pathlib,sys
root=pathlib.Path(sys.argv[1])
for name,expected in [('agent-sandbox.yaml','437e6875cf7dfbd1c1230de3641f73bb9dad595a581366ef7f98f9b2813dd686'),('temporal.tgz','f7b443f637b71fcbddd12900e5be9fe4c18cf741495d7b2cb247670681bf57d4')]:
    if hashlib.sha256((root/name).read_bytes()).hexdigest()!=expected:raise SystemExit('upstream artifact checksum mismatch: '+name)
PY
kubectl --kubeconfig "$kubeconfig" apply -f "$state_dir/agent-sandbox.yaml"
kubectl --kubeconfig "$kubeconfig" -n agent-sandbox-system rollout status deployment/agent-sandbox-controller --timeout=300s
kubectl --kubeconfig "$kubeconfig" apply -f deploy/kind/postgres.yaml
kubectl --kubeconfig "$kubeconfig" -n temporal rollout status statefulset/postgres --timeout=300s
helm upgrade --install temporal "$state_dir/temporal.tgz" --namespace temporal --kubeconfig "$kubeconfig" \
  -f deploy/kind/temporal-values.yaml --wait --timeout 5m
helm upgrade --install openshell oci://ghcr.io/nvidia/openshell/helm-chart --version 0.1.4 \
  --namespace openshell --create-namespace --kubeconfig "$kubeconfig" \
  -f deploy/kind/openshell-values.yaml --wait --timeout 5m
if ! kubectl --kubeconfig "$kubeconfig" -n temporal exec deployment/temporal-admintools -- \
  temporal operator namespace describe --namespace default --address temporal-frontend:7233 >/dev/null 2>&1; then
  kubectl --kubeconfig "$kubeconfig" -n temporal exec deployment/temporal-admintools -- \
    temporal operator namespace create --namespace default --address temporal-frontend:7233
fi
python3 - "$state_dir" <<'PY'
import base64,json,pathlib,subprocess,sys
root=pathlib.Path(sys.argv[1])
data=json.loads(subprocess.check_output(['kubectl','--kubeconfig',str(root/'kubeconfig'),'-n','openshell','get','secret','openshell-client-tls','-o','json']))['data']
for name in ('ca.crt','tls.crt','tls.key'):
    target=root/'tls'/name;target.write_bytes(base64.b64decode(data[name]));target.chmod(0o600)
PY
docker build -t zetra-validation:local .
kind load docker-image zetra-validation:local --name "$cluster_name"
