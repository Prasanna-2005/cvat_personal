#!/usr/bin/env bash
# Build local CVAT images, load them into MicroK8s, and roll out pods.
# Also supports Helm upgrades and Nuclio custom function deploys.
#
# Usage:
#   ./deploy-k8s.sh                  # build server+ui, import, restart app pods
#   ./deploy-k8s.sh images           # same as default
#   ./deploy-k8s.sh images server    # backend only
#   ./deploy-k8s.sh images ui        # frontend only
#   ./deploy-k8s.sh helm             # helm upgrade with values.override.yml
#   ./deploy-k8s.sh functions        # deploy all serverless/custom Nuclio funcs
#   ./deploy-k8s.sh functions skewocr
#   ./deploy-k8s.sh all              # images + helm + functions
#   ./deploy-k8s.sh restart          # restart app pods only (no rebuild)
#
# Flags:
#   --prune-docker   After import, remove the Docker copy of built images
#                    (frees disk; MicroK8s keeps its own copy)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

NS="${CVAT_K8S_NAMESPACE:-cvat}"
SERVER_IMAGE="${CVAT_SERVER_IMAGE:-cvat/server:v2.69.0-local}"
UI_IMAGE="${CVAT_UI_IMAGE:-cvat/ui:2.69.0-local}"
REGISTRY="${NUCTL_REGISTRY:-localhost:32000}"
PRUNE_DOCKER=0

DOCKER=(sudo docker)
KUBECTL=(microk8s kubectl)
HELM=(microk8s helm)

log() { echo "[+] $*"; }
die() { echo "[!] $*" >&2; exit 1; }

usage() {
  sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

ensure_kubeconfig() {
  if [[ -z "${KUBECONFIG:-}" ]]; then
    local cfg="/tmp/microk8s.config.$$"
    microk8s config >"$cfg"
    export KUBECONFIG="$cfg"
  fi
}

load_env() {
  if [[ -f "$ROOT_DIR/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$ROOT_DIR/.env"
    set +a
  fi
}

cmd_build() {
  local target="${1:-all}"
  case "$target" in
    all|images)
      log "Building cvat_server + cvat_ui"
      "${DOCKER[@]}" compose build cvat_server cvat_ui
      ;;
    server|backend)
      log "Building cvat_server → ${SERVER_IMAGE}"
      "${DOCKER[@]}" compose build cvat_server
      ;;
    ui|frontend)
      log "Building cvat_ui → ${UI_IMAGE}"
      "${DOCKER[@]}" compose build cvat_ui
      ;;
    *) die "Unknown build target: $target (use all|server|ui)" ;;
  esac
}

import_image() {
  local image="$1"
  log "Importing ${image} into MicroK8s containerd"
  "${DOCKER[@]}" save "$image" | microk8s ctr image import -
  if [[ "$PRUNE_DOCKER" -eq 1 ]]; then
    log "Pruning Docker copy of ${image}"
    "${DOCKER[@]}" rmi "$image" || true
  fi
}

cmd_import() {
  local target="${1:-all}"
  case "$target" in
    all|images)
      import_image "$SERVER_IMAGE"
      import_image "$UI_IMAGE"
      ;;
    server|backend) import_image "$SERVER_IMAGE" ;;
    ui|frontend) import_image "$UI_IMAGE" ;;
    *) die "Unknown import target: $target" ;;
  esac
}

backend_deploys() {
  "${KUBECTL[@]}" -n "$NS" get deploy -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.template.spec.containers[0].image}{"\n"}{end}' \
    | awk -v img="$SERVER_IMAGE" '$2 == img || $2 == "docker.io/" img { print $1 }'
}

cmd_restart() {
  local target="${1:-all}"
  case "$target" in
    all|images)
      log "Restarting backend deployments using ${SERVER_IMAGE}"
      local d
      while read -r d; do
        [[ -n "$d" ]] || continue
        log "  rollout restart deploy/${d}"
        "${KUBECTL[@]}" -n "$NS" rollout restart "deploy/${d}"
      done < <(backend_deploys)
      log "Restarting frontend"
      "${KUBECTL[@]}" -n "$NS" rollout restart deploy/cvat-frontend
      ;;
    server|backend)
      while read -r d; do
        [[ -n "$d" ]] || continue
        "${KUBECTL[@]}" -n "$NS" rollout restart "deploy/${d}"
      done < <(backend_deploys)
      ;;
    ui|frontend)
      "${KUBECTL[@]}" -n "$NS" rollout restart deploy/cvat-frontend
      ;;
    *) die "Unknown restart target: $target" ;;
  esac

  log "Waiting for rollouts..."
  if [[ "$target" == "ui" || "$target" == "frontend" ]]; then
    "${KUBECTL[@]}" -n "$NS" rollout status deploy/cvat-frontend --timeout=180s
  else
    "${KUBECTL[@]}" -n "$NS" rollout status deploy/cvat-backend-server --timeout=180s
    if [[ "$target" == "all" || "$target" == "images" ]]; then
      "${KUBECTL[@]}" -n "$NS" rollout status deploy/cvat-frontend --timeout=180s || true
    fi
  fi
  "${KUBECTL[@]}" -n "$NS" get pods -o wide
}

cmd_images() {
  local target="${1:-all}"
  cmd_build "$target"
  cmd_import "$target"
  cmd_restart "$target"
}

cmd_helm() {
  log "Helm upgrade cvat in namespace ${NS}"
  "${HELM[@]}" upgrade cvat "$ROOT_DIR/helm-chart" \
    -n "$NS" \
    -f "$ROOT_DIR/helm-chart/values.yaml" \
    -f "$ROOT_DIR/helm-chart/values.override.yml" \
    --timeout 10m
  "${KUBECTL[@]}" -n "$NS" get pods -o wide
}

deploy_one_function() {
  local path="$1"
  [[ -d "$path" ]] || die "Function path not found: $path"
  [[ -f "$path/function.yaml" ]] || die "Missing function.yaml in $path"

  ensure_kubeconfig
  load_env

  # Project may already exist
  sudo -E nuctl create project cvat --platform kube --namespace "$NS" 2>/dev/null || true

  log "Deploying Nuclio function from ${path}"
  sudo -E nuctl deploy --project-name cvat --path "$path" \
    --platform kube --namespace "$NS" \
    --registry "$REGISTRY" \
    --run-registry "$REGISTRY" \
    --env CVAT_FUNCTIONS_REDIS_HOST=cvat-kvrocks \
    --env CVAT_FUNCTIONS_REDIS_PORT=6666 \
    --env FINUIT_OCR_KEY="${FINUIT_OCR_KEY:-}" \
    --env OPENAI_API_KEY="${OPENAI_API_KEY:-}" \
    --env FINUIT_API_KEY="${FINUIT_OCR_KEY:-}" \
    --env MLFLOW_TRACKING_URI="${MLFLOW_TRACKING_URI:-}" \
    --env MLFLOW_TRACKING_USERNAME="${MLFLOW_TRACKING_USERNAME:-}" \
    --env MLFLOW_TRACKING_PASSWORD="${MLFLOW_TRACKING_PASSWORD:-}"
}

cmd_functions() {
  local which="${1:-all}"
  ensure_kubeconfig
  load_env

  if [[ "$which" == "all" ]]; then
    local d
    for d in "$ROOT_DIR"/serverless/custom/*; do
      deploy_one_function "$d"
    done
  else
    local path="$ROOT_DIR/serverless/custom/${which}"
    if [[ ! -d "$path" && -d "$which" ]]; then
      path="$which"
    fi
    deploy_one_function "$path"
  fi

  sudo -E nuctl get function --platform kube --namespace "$NS"
  "${KUBECTL[@]}" -n "$NS" get pods | grep -E 'NAME|nuclio-' || true
}

cmd_all() {
  cmd_images all
  cmd_helm
  cmd_functions all
}

# Parse flags
ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage ;;
    --prune-docker) PRUNE_DOCKER=1; shift ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
set -- "${ARGS[@]:-}"

CMD="${1:-images}"
shift || true
TARGET="${1:-all}"

case "$CMD" in
  images|image|app)   cmd_images "$TARGET" ;;
  build)              cmd_build "$TARGET" ;;
  import)             cmd_import "$TARGET" ;;
  restart)            cmd_restart "$TARGET" ;;
  helm)               cmd_helm ;;
  functions|nuclio|fn) cmd_functions "${1:-all}" ;;
  all)                cmd_all ;;
  help|-h|--help)     usage ;;
  *) die "Unknown command: $CMD (try --help)" ;;
esac

log "Done."
