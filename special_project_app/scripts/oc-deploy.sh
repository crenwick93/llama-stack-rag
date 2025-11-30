#!/usr/bin/env bash
set -euo pipefail

# Usage: ./scripts/oc-deploy.sh <APPS_DOMAIN>
# Example: ./scripts/oc-deploy.sh apps.cluster-xxxx.example.com

APPS_DOMAIN="${1:-}"
if [[ -z "$APPS_DOMAIN" ]]; then
  echo "Provide your OpenShift apps domain. Example:"
  echo "  ./scripts/oc-deploy.sh apps.cluster-xxxx.example.com"
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Load .env if present (for ServiceNow settings, etc.)
if [[ -f "$ROOT/.env" ]]; then
  echo "Loading environment from $ROOT/.env"
  # shellcheck disable=SC2046
  export $(grep -v '^#' "$ROOT/.env" | xargs -I{} echo {})
fi

# Login assumed; project create/apply
oc apply -f "$ROOT/openshift/00-namespace.yaml"
oc label namespace special-payment-project openshift.io/user-monitoring=true --overwrite || true

# BuildConfigs + ImageStreams
oc apply -f "$ROOT/openshift/10-builds-imagestreams.yaml"

# Enable User Workload Monitoring + Alertmanager (requires cluster-admin)
echo "Enabling User Workload Monitoring (cluster-admin required)..."
oc apply -f - <<'EOF' || true
apiVersion: v1
kind: ConfigMap
metadata:
  name: cluster-monitoring-config
  namespace: openshift-monitoring
data:
  config.yaml: |
    enableUserWorkload: true
EOF

# Wait for the user-workload namespace to exist
echo "Waiting for openshift-user-workload-monitoring namespace..."
for i in {1..30}; do
  if oc get ns openshift-user-workload-monitoring >/dev/null 2>&1; then break; fi
  sleep 5
done

# Enable user-workload Alertmanager specifically
echo "Enabling user-workload Alertmanager..."
oc apply -f - <<'EOF' || true
apiVersion: v1
kind: ConfigMap
metadata:
  name: user-workload-monitoring-config
  namespace: openshift-user-workload-monitoring
data:
  config.yaml: |
    alertmanager:
      enabled: true
      enableAlertmanagerConfig: true
EOF

# Start binary builds
echo "Starting frontend build..."
oc start-build -n special-payment-project frontend --from-dir="$ROOT/frontend" --wait --follow
echo "Starting api build..."
oc start-build -n special-payment-project api --from-dir="$ROOT/api" --wait --follow
echo "Starting snow-bridge build..."
oc start-build -n special-monitoring snow-bridge --from-dir="$ROOT/monitoring/snow-bridge" --wait --follow

# Cleanup: remove build pods (noise)
echo "Cleaning up build pods..."
oc delete pod -n special-payment-project -l openshift.io/build.name --ignore-not-found || true

# Deployments + Services
oc apply -f "$ROOT/openshift/20-deploy-services.yaml"

# ExternalName service
oc apply -f "$ROOT/openshift/40-payments-external.yaml"

# Routes (patch host)
tmp=$(mktemp)
sed "s/special-payments.apps.CHANGE-ME.example.com/special-payments.$APPS_DOMAIN/g" "$ROOT/openshift/30-routes.yaml" > "$tmp"
oc apply -f "$tmp"
rm -f "$tmp"

# Monitoring (ServiceMonitor + PrometheusRule)
echo "Applying monitoring resources..."
oc apply -f "$ROOT/monitoring/servicemonitor.yaml"
oc apply -f "$ROOT/monitoring/prometheusrule.yaml"
# Optionally create/update ServiceNow credentials Secret from env
if [[ -n "${SERVICENOW_USERNAME:-}" && -n "${SERVICENOW_PASSWORD:-}" ]]; then
  echo "Ensuring snow-credentials Secret exists (from .env variables)..."
  oc -n special-monitoring create secret generic snow-credentials \
    --from-literal=username="${SERVICENOW_USERNAME}" \
    --from-literal=password="${SERVICENOW_PASSWORD}" \
    --dry-run=client -o yaml | oc apply -f -
fi
# Optionally create/update ServiceNow settings Secret (instance URL) from env
if [[ -n "${SERVICENOW_INSTANCE_URL:-}" ]]; then
  echo "Ensuring snow-settings Secret exists (from .env variables)..."
  oc -n special-monitoring create secret generic snow-settings \
    --from-literal=instance_url="${SERVICENOW_INSTANCE_URL}" \
    --dry-run=client -o yaml | oc apply -f -
fi
# Apply AlertmanagerConfig; if SERVICENOW_ALERT_WEBHOOK_URL provided, substitute placeholder
if [[ -f "$ROOT/monitoring/alertmanagerconfig.yaml" ]]; then
  if [[ -n "${SERVICENOW_ALERT_WEBHOOK_URL:-}" ]]; then
    echo "Applying AlertmanagerConfig with ServiceNow webhook URL from .env..."
    tmp_amc=$(mktemp)
    sed "s#https://YOUR_INSTANCE.service-now.com/api/YOUR_SCOPE/YOUR_API#${SERVICENOW_ALERT_WEBHOOK_URL}#g" \
      "$ROOT/monitoring/alertmanagerconfig.yaml" > "$tmp_amc"
    oc apply -f "$tmp_amc"
    rm -f "$tmp_amc"
  else
    echo "Applying AlertmanagerConfig (no SERVICENOW_ALERT_WEBHOOK_URL provided; using file as-is)..."
    oc apply -f "$ROOT/monitoring/alertmanagerconfig.yaml"
    echo "Tip: set SERVICENOW_ALERT_WEBHOOK_URL in $ROOT/.env to auto-substitute your endpoint."
  fi
else
  echo "Skipping AlertmanagerConfig (not found at $ROOT/monitoring/alertmanagerconfig.yaml)."
fi

# Wait for user-workload Alertmanager (if enabled)
echo "Waiting for user-workload Alertmanager to be ready..."
oc -n openshift-user-workload-monitoring rollout status statefulset/alertmanager-user-workload --timeout=300s || true

# Force rollouts to ensure latest images are pulled
echo "Forcing rollouts to pull latest images..."
oc -n special-payment-project rollout restart deploy/checkout-api || true
oc -n special-payment-project rollout restart deploy/checkout-frontend || true
oc -n special-monitoring rollout restart deploy/snow-bridge || true

echo "Waiting for rollouts..."
oc -n special-payment-project rollout status deploy/checkout-api --timeout=180s || true
oc -n special-payment-project rollout status deploy/checkout-frontend --timeout=180s || true
oc -n special-monitoring rollout status deploy/snow-bridge --timeout=180s || true

echo
echo "Open the site:"
echo "  https://special-payments.$APPS_DOMAIN"
echo
echo "Click 'Pay £1.00' — should succeed (200)."
echo
echo "Observability:"
echo "- In the OpenShift Console -> Developer or Admin perspective -> Observe -> Metrics:"
echo "  Query: special_project_upstream_ok (namespace: special-payment-project)"
echo "- Observe -> Alerts: look for 'PaymentUpstream' when the upstream is down."


