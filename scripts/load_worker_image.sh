#!/usr/bin/env bash
# Import only into the named disposable kind node, preserving the OCI digest.
set -euo pipefail
zetra_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$zetra_root"
zetra_connection="${ZETRA_PODMAN_CONNECTION:-zetra-validation-root}"
zetra_podman="${ZETRA_PODMAN_BIN:-podman}"
zetra_kind="${ZETRA_KIND_BIN:-kind}"
zetra_podman_path="$(command -v "$zetra_podman")"
PATH="$(dirname "$zetra_podman_path"):$PATH"
export PATH
export KIND_EXPERIMENTAL_PROVIDER=podman
CONTAINER_HOST="$("$zetra_podman" system connection list --format json | python3 -c \
  'import json,sys; print(next(x["URI"] for x in json.load(sys.stdin) if x["Name"]==sys.argv[1]))' "$zetra_connection")"
CONTAINER_SSHKEY="$("$zetra_podman" system connection list --format json | python3 -c \
  'import json,sys; print(next(x["Identity"] for x in json.load(sys.stdin) if x["Name"]==sys.argv[1]))' "$zetra_connection")"
export CONTAINER_HOST CONTAINER_SSHKEY
mkdir -p .tools
"$zetra_podman" --connection "$zetra_connection" save --format oci-archive \
  --output .tools/worker-image.oci.tar localhost/zetra-worker:0.1-lab
"$zetra_kind" load image-archive .tools/worker-image.oci.tar --name zetra-validation
zetra_digest="$(python3 -c \
  'import hashlib,tarfile; a=tarfile.open(".tools/worker-image.oci.tar"); print("sha256:"+hashlib.sha256(a.extractfile("index.json").read()).hexdigest())')"
if [[ ! "$zetra_digest" =~ ^sha256:[a-f0-9]{64}$ ]]; then
  printf 'Invalid OCI manifest digest\n' >&2
  exit 1
fi
zetra_reference="localhost/zetra-worker@$zetra_digest"
"$zetra_podman" --connection "$zetra_connection" exec zetra-validation-control-plane \
  mkdir -p /tmp/zetra-worker-import
"$zetra_podman" --connection "$zetra_connection" cp .tools/worker-image.oci.tar \
  zetra-validation-control-plane:/tmp/zetra-worker-import/image.oci.tar
"$zetra_podman" --connection "$zetra_connection" exec zetra-validation-control-plane \
  ctr -n k8s.io images import --label io.cri-containerd.image=managed \
  --index-name "$zetra_reference" /tmp/zetra-worker-import/image.oci.tar
"$zetra_podman" --connection "$zetra_connection" exec zetra-validation-control-plane \
  rm /tmp/zetra-worker-import/image.oci.tar
"$zetra_podman" --connection "$zetra_connection" exec zetra-validation-control-plane \
  crictl inspecti "$zetra_reference" > .tools/worker-kind-inspect.json
"$zetra_podman" --connection "$zetra_connection" exec zetra-validation-control-plane \
  ctr -n k8s.io images list "name==$zetra_reference" > .tools/worker-kind-image-target.txt
"$zetra_podman" --connection "$zetra_connection" exec zetra-validation-control-plane \
  ctr -n k8s.io content get "$zetra_digest" > .tools/worker-kind-index.json
python3 -c \
  'import hashlib,sys; from pathlib import Path; d=sys.argv[1]; rows=Path(".tools/worker-kind-image-target.txt").read_text().splitlines(); assert len(rows)==2 and rows[1].split()[2]==d; assert "sha256:"+hashlib.sha256(Path(".tools/worker-kind-index.json").read_bytes()).hexdigest()==d' \
  "$zetra_digest"
printf '%s\n' "$zetra_reference" > .tools/worker-kind-reference
printf 'Loaded %s in kind-zetra-validation; no pod launched\n' "$zetra_reference"
