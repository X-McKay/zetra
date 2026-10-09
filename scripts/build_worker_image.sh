#!/usr/bin/env bash
# Build only into the explicit disposable Podman connection; never push.
set -euo pipefail
zetra_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$zetra_root"
zetra_connection="${ZETRA_PODMAN_CONNECTION:-zetra-validation-root}"
zetra_tag="localhost/zetra-worker:0.1-lab"
zetra_podman="${ZETRA_PODMAN_BIN:-podman}"
mkdir -p .tools
"$zetra_podman" --connection "$zetra_connection" info >/dev/null
"$zetra_podman" --connection "$zetra_connection" build \
  --platform linux/arm64 --format oci --pull=missing \
  --file deploy/Dockerfile --tag "$zetra_tag" \
  --iidfile .tools/worker-image-id .
"$zetra_podman" --connection "$zetra_connection" image inspect "$zetra_tag" \
  > .tools/worker-image-inspect.json
printf 'Built %s in connection %s; inspect .tools/worker-image-inspect.json\n' \
  "$zetra_tag" "$zetra_connection"
