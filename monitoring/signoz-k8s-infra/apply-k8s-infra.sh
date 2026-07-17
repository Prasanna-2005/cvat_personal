#!/usr/bin/env bash
# Upgrade SigNoz k8s-infra and re-apply hostNetwork on otel-deployment
# (chart exposes hostNetwork only for otelAgent).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -f "${SCRIPT_DIR}/.env" ]]; then
  echo "Missing ${SCRIPT_DIR}/.env — copy from .env.template first:" >&2
  echo "  cp ${SCRIPT_DIR}/.env.template ${SCRIPT_DIR}/.env" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/.env"
set +a

: "${CENTRAL_OTEL_ENDPOINT:?}"
: "${K8S_CLUSTER_NAME:?}"
: "${ENVIRONMENT:?}"
: "${HELM_RELEASE:=signoz-k8s-infra}"
: "${HELM_NAMESPACE:=platform-monitoring}"
: "${PROMETHEUS_SCRAPE_NAMESPACES:=cvat}"

# Helm --set for list: namespaces={cvat} or namespaces={a,b}
NS_SET=$(printf '%s' "${PROMETHEUS_SCRAPE_NAMESPACES}" | tr ',' '\n' | sed '/^$/d' | paste -sd, -)
NS_SET="{${NS_SET}}"

microk8s helm upgrade --install "${HELM_RELEASE}" signoz/k8s-infra \
  -n "${HELM_NAMESPACE}" --create-namespace \
  -f "${SCRIPT_DIR}/k8s-infra-values.yaml" \
  --set "otelCollectorEndpoint=${CENTRAL_OTEL_ENDPOINT}" \
  --set "global.clusterName=${K8S_CLUSTER_NAME}" \
  --set "global.deploymentEnvironment=${ENVIRONMENT}" \
  --set "presets.prometheus.namespaces=${NS_SET}" \
  --timeout 5m

microk8s kubectl -n "${HELM_NAMESPACE}" patch deployment "${HELM_RELEASE}-otel-deployment" \
  --patch-file "${SCRIPT_DIR}/otel-deployment-hostnetwork-patch.yaml"

microk8s kubectl -n "${HELM_NAMESPACE}" patch daemonset "${HELM_RELEASE}-otel-agent" --type=merge \
  -p '{"spec":{"template":{"spec":{"dnsPolicy":"ClusterFirstWithHostNet"}}}}'

microk8s kubectl -n "${HELM_NAMESPACE}" rollout status "daemonset/${HELM_RELEASE}-otel-agent" --timeout=120s
microk8s kubectl -n "${HELM_NAMESPACE}" rollout status "deployment/${HELM_RELEASE}-otel-deployment" --timeout=120s
microk8s kubectl -n "${HELM_NAMESPACE}" get pods -o wide
