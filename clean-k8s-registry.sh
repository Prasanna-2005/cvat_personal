#!/usr/bin/env bash
# Clean / inspect the MicroK8s registry (localhost:32000 by default).
#
# Usage:
#   ./clean-k8s-registry.sh list
#   ./clean-k8s-registry.sh list cvat.python.external.ocr
#   ./clean-k8s-registry.sh delete test/hello              # all tags
#   ./clean-k8s-registry.sh delete test/hello latest      # one tag
#   ./clean-k8s-registry.sh gc                            # garbage-collect blobs
#   ./clean-k8s-registry.sh prune-unused                  # delete repos not used by pods, then gc
#   ./clean-k8s-registry.sh prune-unused --dry-run
#
# Env:
#   REGISTRY_URL=http://localhost:32000
#   REGISTRY_NAMESPACE=container-registry
set -euo pipefail

REGISTRY_URL="${REGISTRY_URL:-http://localhost:32000}"
REGISTRY_NS="${REGISTRY_NAMESPACE:-container-registry}"
DRY_RUN=0
KUBECTL=(microk8s kubectl)

log() { echo "[+] $*"; }
warn() { echo "[!] $*" >&2; }
die() { echo "[!] $*" >&2; exit 1; }

usage() {
  sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

registry_get() {
  curl -sS --fail-with-body "${REGISTRY_URL}$1"
}

registry_head() {
  curl -sS -I --fail-with-body "${REGISTRY_URL}$1"
}

list_repos() {
  registry_get /v2/_catalog | python3 -c 'import json,sys; print("\n".join(json.load(sys.stdin).get("repositories") or []))'
}

list_tags() {
  local repo="$1"
  registry_get "/v2/${repo}/tags/list" | python3 -c 'import json,sys
d=json.load(sys.stdin)
tags=d.get("tags") or []
print("\n".join(tags) if tags else "")'
}

digest_for_tag() {
  local repo="$1" tag="$2"
  curl -sS -I \
    -H 'Accept: application/vnd.docker.distribution.manifest.v2+json' \
    -H 'Accept: application/vnd.oci.image.manifest.v1+json' \
    -H 'Accept: application/vnd.oci.image.index.v1+json' \
    "${REGISTRY_URL}/v2/${repo}/manifests/${tag}" \
    | tr -d '\r' \
    | awk -F': ' 'tolower($1)=="docker-content-digest"{print $2; exit}'
}

delete_manifest() {
  local repo="$1" digest="$2"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "DRY-RUN delete ${repo}@${digest}"
    return 0
  fi
  curl -sS -X DELETE --fail-with-body \
    "${REGISTRY_URL}/v2/${repo}/manifests/${digest}" >/dev/null
  log "Deleted ${repo}@${digest}"
}

cmd_list() {
  local repo="${1:-}"
  if [[ -z "$repo" ]]; then
    log "Repositories at ${REGISTRY_URL}"
    local r
    while read -r r; do
      [[ -n "$r" ]] || continue
      local tags
      tags=$(list_tags "$r" | tr '\n' ' ' || true)
      echo "  ${r}: ${tags:-<no tags>}"
    done < <(list_repos)
  else
    log "Tags for ${repo}"
    list_tags "$repo" | sed 's/^/  /'
  fi
}

cmd_delete() {
  local repo="${1:-}"
  local tag="${2:-}"
  [[ -n "$repo" ]] || die "Usage: $0 delete <repo> [tag]"

  if [[ -n "$tag" ]]; then
    local digest
    digest=$(digest_for_tag "$repo" "$tag")
    [[ -n "$digest" ]] || die "Could not resolve digest for ${repo}:${tag}"
    delete_manifest "$repo" "$digest"
  else
    local t
    while read -r t; do
      [[ -n "$t" ]] || continue
      local digest
      digest=$(digest_for_tag "$repo" "$t")
      [[ -n "$digest" ]] || { warn "Skip ${repo}:${t} (no digest)"; continue; }
      delete_manifest "$repo" "$digest"
    done < <(list_tags "$repo")
  fi
}

registry_pod() {
  "${KUBECTL[@]}" -n "$REGISTRY_NS" get pod -l app=registry \
    -o jsonpath='{.items[0].metadata.name}'
}

cmd_gc() {
  local pod
  pod=$(registry_pod)
  [[ -n "$pod" ]] || die "No registry pod in ${REGISTRY_NS}"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "DRY-RUN garbage-collect on pod/${pod}"
    return 0
  fi
  log "Garbage-collecting unreferenced blobs on pod/${pod}"
  "${KUBECTL[@]}" -n "$REGISTRY_NS" exec "$pod" -- \
    registry garbage-collect /etc/docker/registry/config.yml
  log "GC finished"
}

# Repos referenced by running pods (image refs like localhost:32000/foo:tag)
in_use_repos() {
  "${KUBECTL[@]}" get pods -A -o jsonpath='{range .items[*]}{range .spec.containers[*]}{.image}{"\n"}{end}{range .spec.initContainers[*]}{.image}{"\n"}{end}{end}' \
    | sed -n 's|^[^/]*localhost:32000/\([^:@]*\).*|\1|p' \
    | sort -u
}

cmd_prune_unused() {
  log "Comparing registry catalog to images used by pods"
  local used
  used=$(in_use_repos)
  echo "In use:"
  if [[ -z "$used" ]]; then
    echo "  (none from ${REGISTRY_URL})"
  else
    echo "$used" | sed 's/^/  /'
  fi

  local to_delete=()
  local r
  while read -r r; do
    [[ -n "$r" ]] || continue
    if ! grep -qxF "$r" <<<"$used"; then
      to_delete+=("$r")
    fi
  done < <(list_repos)

  if [[ ${#to_delete[@]} -eq 0 ]]; then
    log "Nothing unused to delete"
    return 0
  fi

  echo "Unused (will delete):"
  printf '  %s\n' "${to_delete[@]}"

  if [[ "$DRY_RUN" -eq 0 ]]; then
    read -r -p "Proceed? [y/N] " ans
    [[ "${ans:-}" =~ ^[Yy]$ ]] || { log "Aborted"; return 0; }
  fi

  for r in "${to_delete[@]}"; do
    cmd_delete "$r"
  done
  cmd_gc
}

# Flags
ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
set -- "${ARGS[@]:-}"

CMD="${1:-list}"
shift || true

case "$CMD" in
  list)          cmd_list "${1:-}" ;;
  delete|rm)     cmd_delete "${1:-}" "${2:-}" ;;
  gc|garbage-collect) cmd_gc ;;
  prune-unused|prune) cmd_prune_unused ;;
  help|-h|--help) usage ;;
  *) die "Unknown command: $CMD (try --help)" ;;
esac
