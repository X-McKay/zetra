#!/usr/bin/env bash
# Release one platform image: refuse unless the build is safe, then build it exactly as the
# pull request job does, push it as <registry>/<image>:<first 7 of the commit>, and report the
# reference with its digest. Phase 2 signs that digest; nothing here signs.
#
#   release.sh config              the docker client configuration holds the registry login
#   release.sh preflight <image>   entry, pushed commit, ancestry of origin/main, clean tree
#   release.sh tag                 print the tag for the commit being built
#   release.sh push <image>        build, push, read the digest back, write summary and outputs
#   release.sh all <image>         config, preflight, push (what the composite action runs)
#
# Every decision is in this file so a test can run it with a fake `docker` on PATH.
#
# Environment:
#   GITHUB_SHA / GITHUB_EVENT_NAME / GITHUB_REF   set by Actions; the pushed commit and event
#   PLATFORM_REGISTRY   default registry.almckay.io/platform
#   PLATFORM_PYTHON     interpreter for images.py, default python3 (words allowed: `uv run ...`)
#   IMAGES_PY           default <repo>/platform/contracts/agent_platform/images.py
#   DOCKER_CONFIG       docker client configuration directory, default ~/.docker
#   GITHUB_OUTPUT, GITHUB_STEP_SUMMARY   optional; where the result is written
set -euo pipefail

registry=${PLATFORM_REGISTRY:-registry.almckay.io/platform}
host=${registry%%/*}
read -r -a py <<<"${PLATFORM_PYTHON:-python3}"

die() { echo "error: $*" >&2; exit 1; }

# Scratch files (the build's metadata, a captured error) live here and go when the script ends.
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

check_name() { [[ $1 =~ ^[a-z0-9][a-z0-9-]*$ ]] || die "bad image name: $1"; }

images_py() {
  local script=${IMAGES_PY:-$(git rev-parse --show-toplevel)/platform/contracts/agent_platform/images.py}
  PYTHONDONTWRITEBYTECODE=1 "${py[@]}" -I "$script" "$@"
}

# The client configuration is mounted by the cluster from a Secret, so there is no login
# step and no secret here. Only that one file is read-only: the docker client and buildx
# write their own state (buildx instances, activity records) under the same directory, which
# is why the directory itself must stay writable. The file is never printed.
check_config() {
  local file=${DOCKER_CONFIG:-$HOME/.docker}/config.json
  if ! images_py registry-auth "$file" "$host"; then
    die "the runner has no client configuration for $host (looked in $file).
This job performs no registry login and reads no secret; the cluster mounts the configuration into
the runner. See the operator steps in the arc-runners README of the infrastructure repository."
  fi
}

tag_for() { # <commit>
  [[ $1 =~ ^[0-9a-f]{40}$ ]] || die "not a full commit hash: $1"
  echo "${1:0:7}"
}

preflight() { # <image>
  local image=${1:?usage: release.sh preflight <image>}
  check_name "$image"
  images_py entry "$image" >/dev/null || die "$image is not an entry in platform/images.json"

  [[ ${GITHUB_EVENT_NAME:-} == push ]] || die "releases run only for a push, not '${GITHUB_EVENT_NAME:-}'"
  [[ ${GITHUB_REF:-} == refs/heads/main ]] || die "releases run only for refs/heads/main, not '${GITHUB_REF:-}'"

  local pushed=${GITHUB_SHA:?GITHUB_SHA is not set} head
  head=$(git rev-parse HEAD)
  [[ $head == "$pushed" ]] || die "the checkout is $head, not the pushed commit $pushed"

  # A shallow checkout cannot answer an ancestry question, so fetch what is needed. Run
  # from a clone made by actions/checkout with persist-credentials: false; zetra is public.
  if [[ $(git rev-parse --is-shallow-repository) == true ]]; then
    git fetch --no-tags --unshallow origin '+refs/heads/main:refs/remotes/origin/main'
  else
    git fetch --no-tags origin '+refs/heads/main:refs/remotes/origin/main'
  fi
  git merge-base --is-ancestor "$pushed" refs/remotes/origin/main \
    || die "commit $pushed is not an ancestor of origin/main"

  # Ignored files count too: the build context is the directory, whatever .gitignore says.
  [[ -z $(git status --porcelain --untracked-files=all --ignored) ]] \
    || die "the checkout has uncommitted, untracked or ignored files; a release must be a commit"
}

# Prints the digest of a reference, or nothing when the tag does not exist. "Does not exist"
# is exactly the line buildx prints for a reference the registry answers 404 for. Any other
# failure stops the release rather than reading as "absent": authentication, the network, a
# missing credential helper, or a part of an existing image that cannot be found.
registry_digest() { # <reference>
  local out err=$work/inspect.err status=0
  out=$(docker buildx imagetools inspect "$1" --format '{{.Manifest.Digest}}' 2>"$err") || status=$?
  if ((status != 0)); then
    if grep -qxF "ERROR: $1: not found" "$err"; then return 0; fi
    cat "$err" >&2
    die "could not inspect $1"
  fi
  echo "$out"
}

# The Dockerfile and the context must be inside the checkout once links are followed, so a
# build cannot be pointed at a directory of the runner (its docker configuration, say).
inside_checkout() { # <what> <path as the build will read it>
  local top real
  top=$(realpath -e -- "$(git rev-parse --show-toplevel)")
  real=$(realpath -e -- "$2") || die "$1 $2 does not exist in the checkout"
  [[ $real == "$top" || $real == "$top"/* ]] || die "$1 $2 leads outside the checkout"
}

push() { # <image>
  local image=${1:?usage: release.sh push <image>}
  local sha=${GITHUB_SHA:?GITHUB_SHA is not set} tag ref existing
  check_name "$image"
  tag=$(tag_for "$sha")
  ref=$registry/$image:$tag

  existing=$(registry_digest "$ref")
  if [[ -n $existing ]]; then
    die "$ref already exists as $existing; refusing to overwrite it. A release is one image per
commit and the tag is never moved. If this run is a retry of one that pushed, that digest is
the image."
  fi

  local -a fields
  images_py entry "$image" >"$work/entry" || die "$image is not a usable entry in platform/images.json"
  mapfile -d '' -t fields <"$work/entry"
  ((${#fields[@]} >= 3)) || die "the entry for $image is incomplete"
  local dockerfile=${fields[0]} context=${fields[1]} platform=${fields[2]}
  inside_checkout "the Dockerfile" "$dockerfile"
  inside_checkout "the build context" "$context"
  local -a args=()
  local arg
  for arg in "${fields[@]:3}"; do args+=(--build-arg "$arg"); done

  local meta=$work/metadata.json
  # The same flags as the `images` job in .github/workflows/platform.yml, plus the push. The
  # context follows `--` so it can never be read as an option.
  docker buildx build --pull --no-cache --platform "$platform" ${args[@]+"${args[@]}"} \
    -f "$dockerfile" -t "$ref" --push --metadata-file "$meta" -- "$context"

  local digest readback
  digest=$(images_py metadata-digest "$meta")
  readback=$(registry_digest "$ref")
  if [[ $readback != "$digest" ]]; then
    die "$ref now resolves to '${readback:-nothing}' but this build reported $digest. The image
was pushed, so a re-run of this job will refuse; look at the tag before anything pins it."
  fi

  local full=$ref@$digest
  echo "pushed $full"
  if [[ -n ${GITHUB_OUTPUT:-} ]]; then
    {
      echo "reference=$full"
      echo "digest=$digest"
      echo "tag=$tag"
    } >>"$GITHUB_OUTPUT"
  fi
  if [[ -n ${GITHUB_STEP_SUMMARY:-} ]]; then
    # shellcheck disable=SC2016 # the backticks are literal Markdown
    printf '### %s\n\n`%s`\n\nBuilt from `%s`, not signed yet.\n\n' "$image" "$full" "$sha" >>"$GITHUB_STEP_SUMMARY"
  fi
}

case ${1:-} in
  config) check_config ;;
  preflight) preflight "${2:-}" ;;
  tag) tag_for "${GITHUB_SHA:?GITHUB_SHA is not set}" ;;
  push) push "${2:-}" ;;
  all) check_config; preflight "${2:-}"; push "${2:-}" ;;
  *) die "usage: release.sh config|preflight <image>|tag|push <image>|all <image>" ;;
esac
