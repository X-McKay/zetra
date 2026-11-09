#!/usr/bin/env bash
# Build, push, sign and attest one agent image, then write its release artifacts
# (spec 14.1). Run by the agent-release composite action on an `<agent>/v*` tag, from
# the root of an agent repository laid out as zetra_agents.layout requires.
#
# Signing is keyless against Kubani's PRIVATE Sigstore: Fulcio certifies the workflow's
# GitHub Actions OIDC identity, Rekor logs every signature and attestation. There is no
# signing key anywhere. Fulcio accepts only GitHub Actions tokens, so a release can only
# be signed from the workflow; elsewhere the script stops before building.
#
# The image gets a signature and the five attestations Kubani's Kyverno Rule 1 requires,
# all from the same identity: SLSA provenance v1, an SPDX SBOM (syft), a cosign vuln/v1
# scan (grype), agentplatform eval/v1 (the agent's tests) and agentplatform release/v1
# (manifestSha256, policySha256, lockSha256). Then it verifies all six against the pinned
# roots and the identity Kubani pins for the tenant, and runs the eval/vuln conditions
# admission applies.
#
# Needs on PATH: docker, cosign, syft, grype, uv. The repository's locked dev group
# provides zetra-agents and agentctl.
# Environment:
#   ACTIONS_ID_TOKEN_REQUEST_URL/_TOKEN  set by Actions with `permissions: id-token: write`
#   SIGSTORE_FULCIO_URL  default https://fulcio.almckay.io
#   SIGSTORE_REKOR_URL   default https://rekor.almckay.io
#   SIGSTORE_TRUST_DIR   fulcio-roots.pem, rekor.pub, ctlog.pub (the action ships them)
#   VULN_EXCEPTIONS      comma-separated High finding ids the tenant allows (as in
#                        Kubani kyverno/tenant-signers.yaml); Critical is never allowed
#   AGENT_REGISTRY       default registry.almckay.io/agents (what admission accepts)
#   REVISION             default: git HEAD
#   OUT                  default: dist/<agent>
set -euo pipefail

agent=${1:?usage: release.sh <agent>}
root=$(git rev-parse --show-toplevel)
cd "$root"
registry=${AGENT_REGISTRY:-registry.almckay.io/agents}
revision=${REVISION:-$(git rev-parse HEAD)}
out=${OUT:-dist/$agent}
fulcio=${SIGSTORE_FULCIO_URL:-https://fulcio.almckay.io}
rekor=${SIGSTORE_REKOR_URL:-https://rekor.almckay.io}
trust=$(cd "${SIGSTORE_TRUST_DIR:-$(dirname "$0")/sigstore}" && pwd)
issuer=https://token.actions.githubusercontent.com

: "${ACTIONS_ID_TOKEN_REQUEST_URL:?keyless signing needs the workflow OIDC token (permissions: id-token: write)}"
: "${ACTIONS_ID_TOKEN_REQUEST_TOKEN:?keyless signing needs the workflow OIDC token}"
: "${GITHUB_REPOSITORY:?run from GitHub Actions}"
: "${GITHUB_WORKFLOW_REF:?run from GitHub Actions}"

# Only the private Sigstore: these make cosign skip public TUF and trust nothing else.
export SIGSTORE_ROOT_FILE="$trust/fulcio-roots.pem"
export SIGSTORE_REKOR_PUBLIC_KEY="$trust/rekor.pub"
export SIGSTORE_CT_LOG_PUBLIC_KEY_FILE="$trust/ctlog.pub"
export TUF_ROOT="${RUNNER_TEMP:-$(mktemp -d)}/tuf"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "the checkout has uncommitted changes; a release must be a commit" >&2
  exit 1
fi

tools() { uv run --locked zetra-agents "$@"; }
py() { uv run --locked python -c "$@"; }
tools check "$agent"
tools render --check

# The identity Kubani pins for this agent's tenant (agentctl signer-annotations).
workflow=$(basename "${GITHUB_WORKFLOW_REF%%@*}")
subject_regexp=$(py 'import sys; from agent_platform.signing import subject_regexp_for as s
print(s(sys.argv[1], sys.argv[2], (sys.argv[3],)))' "$GITHUB_REPOSITORY" "$workflow" "$agent")
identity="https://github.com/$GITHUB_WORKFLOW_REF"
py 'import re, sys; sys.exit(0 if re.fullmatch(sys.argv[1], sys.argv[2]) else
"this run would sign as " + sys.argv[2] + ", which the tenant pin " + sys.argv[1]
+ " rejects; release from an <agent>/v<version> tag")' "$subject_regexp" "$identity"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$out"

# eval/v1: the agent's own tests, at this commit. A failing suite stops the release.
uv run --locked pytest -q "agents/$agent" --junitxml="$work/junit.xml"
tools predicate eval --junit "$work/junit.xml" --suite "agents/$agent/tests" \
  --commit "$revision" --out "$out/eval.json"

target=$(tools image-target "$agent")
tag="$registry/$agent:${revision:0:12}"
docker build --pull --platform linux/amd64 \
  --build-arg "AGENT=$agent" --target "$target" \
  --label "org.opencontainers.image.revision=$revision" \
  --label "org.opencontainers.image.source=https://github.com/$GITHUB_REPOSITORY" \
  -t "$tag" .
docker push "$tag"
image=$(docker inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$tag" \
  | grep -m1 "^$registry/$agent@sha256:")
echo "pushed $image"

tools release "$agent" --image "$image" --revision "$revision" --out "$out"

# SPDX SBOM of the pushed digest, and grype's scan of that SBOM. The vuln attestation
# (what admission judges) holds only findings that have a fix: a finding the distribution
# will not fix cannot be acted on by a rebuild. The complete scan is published next to it
# as vuln-full.json for review.
syft scan "registry:$image" -q \
  -o "spdx-json=$out/sbom.spdx.json" -o "syft-json=$work/sbom.syft.json"
started=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)
grype "sbom:$work/sbom.syft.json" -q -o json --only-fixed --file "$work/grype.json"
finished=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)
grype "sbom:$work/sbom.syft.json" -q -o json --file "$out/vuln-full.json"
tools predicate vuln --grype "$work/grype.json" --started "$started" --finished "$finished" \
  --out "$out/vuln.json"
tools predicate provenance --revision "$revision" --out "$out/provenance.json"

# A fresh token for each certificate: GitHub's tokens are short-lived.
id_token() {
  curl -fsS -H "Authorization: bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
    "$ACTIONS_ID_TOKEN_REQUEST_URL&audience=sigstore" \
    | py 'import json, sys; print(json.load(sys.stdin)["value"])'
}
keyless=(--yes --fulcio-url "$fulcio" --rekor-url "$rekor")

# cosign --type | predicate file | predicate type Kyverno requires
attestations=(
  "slsaprovenance1|$out/provenance.json|https://slsa.dev/provenance/v1"
  "spdxjson|$out/sbom.spdx.json|https://spdx.dev/Document"
  "vuln|$out/vuln.json|https://cosign.sigstore.dev/attestation/vuln/v1"
  "https://agentplatform.io/eval/v1|$out/eval.json|https://agentplatform.io/eval/v1"
  "https://agentplatform.io/attestations/release/v1|$out/predicate.json|https://agentplatform.io/attestations/release/v1"
)

cosign sign "${keyless[@]}" --identity-token "$(id_token)" "$image"
for entry in "${attestations[@]}"; do
  IFS='|' read -r type file _ <<<"$entry"
  cosign attest "${keyless[@]}" --identity-token "$(id_token)" \
    --type "$type" --predicate "$file" "$image"
done

verify=(--rekor-url "$rekor" --certificate-identity-regexp "$subject_regexp"
  --certificate-oidc-issuer "$issuer")
cosign verify "${verify[@]}" "$image" >/dev/null
for entry in "${attestations[@]}"; do
  IFS='|' read -r _ _ ptype <<<"$entry"
  cosign verify-attestation "${verify[@]}" --type "$ptype" "$image" >/dev/null
  echo "verified $ptype"
done
echo "verified signature and attestations as $identity"

# What Kyverno's attestation conditions check: tests passed, no Critical, High only by
# exception. Signed regardless (a scan is a fact); a failure here stops the publication.
exceptions=()
IFS=',' read -ra ids <<<"${VULN_EXCEPTIONS:-}"
for id in "${ids[@]}"; do [[ -n "${id// /}" ]] && exceptions+=(--vuln-exception "${id// /}"); done
uv run --locked agentctl validate \
  --agent "$out/agent.yaml" --release "$out/agentrelease.yaml" \
  --mcp-resource-base "${AGENT_PLATFORM_MCP_RESOURCE_BASE:-https://mcp.almckay.io}" \
  --eval "$out/eval.json" --vuln "$out/vuln.json" "${exceptions[@]}"

echo "$image" > "$out/image"
echo "release artifacts in $out"
