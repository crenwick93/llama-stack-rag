#!/usr/bin/env bash
set -euo pipefail

# Deploy Llama Stack Distribution on OpenShift using the included templates.
# Usage:
#   ./openshift/llama-stack/oc-deploy.sh --vllm-url https://vllm.example.com \
#       [--namespace llama-stack-demo] [--lsd-name lsd-llama-milvus-inline] \
#       [--model mistral-small-24b-w8a8] [--vllm-token YOUR_TOKEN] \
#       [--tls-verify true|false] [--with-mcp] [--mcp-target-ns special-payment-project]
#
# You can also provide values via $REPO_ROOT/.env:
#   VLLM_URL, VLLM_API_TOKEN, LSD_NAME, NAMESPACE, INFERENCE_MODEL, VLLM_TLS_VERIFY
#
# Example:
#   ./openshift/llama-stack/oc-deploy.sh \
#     --vllm-url https://vllm.apps.cluster-xxxx.example.com \
#     --vllm-token "abc123" \
#     --namespace llama-stack-demo \
#     --with-mcp --mcp-target-ns special-payment-project

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${THIS_DIR}/../.." && pwd)"

# Defaults (can be overridden by flags or .env)
NAMESPACE="${NAMESPACE:-llama-stack-demo}"
LSD_NAME="${LSD_NAME:-lsd-llama-milvus-inline}"
INFERENCE_MODEL="${INFERENCE_MODEL:-mistral-small-24b-w8a8}"
VLLM_URL="${VLLM_URL:-}"
VLLM_API_TOKEN="${VLLM_API_TOKEN:-}"
VLLM_TLS_VERIFY="${VLLM_TLS_VERIFY:-true}"
WITH_MCP="${WITH_MCP:-false}"
MCP_TARGET_NS="${MCP_TARGET_NS:-special-payment-project}"

TEMPLATE_LSD="${REPO_ROOT}/openshift/llama-stack/template.yaml"
TEMPLATE_MCP="${REPO_ROOT}/openshift_mcp_server/template.yaml"

# Load .env if present at repo root
if [[ -f "${REPO_ROOT}/.env" ]]; then
  echo "Loading environment from ${REPO_ROOT}/.env"
  # shellcheck disable=SC2046
  export $(grep -v '^#' "${REPO_ROOT}/.env" | xargs -I{} echo {})
  # Re-evaluate defaults if .env provided values
  NAMESPACE="${NAMESPACE:-llama-stack-demo}"
  LSD_NAME="${LSD_NAME:-lsd-llama-milvus-inline}"
  INFERENCE_MODEL="${INFERENCE_MODEL:-mistral-small-24b-w8a8}"
  VLLM_URL="${VLLM_URL:-}"
  VLLM_API_TOKEN="${VLLM_API_TOKEN:-}"
  VLLM_TLS_VERIFY="${VLLM_TLS_VERIFY:-true}"
  WITH_MCP="${WITH_MCP:-false}"
  MCP_TARGET_NS="${MCP_TARGET_NS:-special-payment-project}"
fi

print_usage() {
  cat <<EOF
Usage: $0 --vllm-url <URL> [options]

Required:
  --vllm-url URL                vLLM base URL (e.g. https://vllm.apps.example.com)

Optional:
  --namespace NAME              OpenShift namespace (default: ${NAMESPACE})
  --lsd-name NAME               LlamaStackDistribution name (default: ${LSD_NAME})
  --model ID                    Inference model id (default: ${INFERENCE_MODEL})
  --vllm-token TOKEN            vLLM API token (if your endpoint requires it)
  --tls-verify true|false       Verify TLS to vLLM (default: ${VLLM_TLS_VERIFY})
  --with-mcp                    Also deploy Kubernetes MCP Server in the same namespace
  --mcp-target-ns NAME          Namespace MCP can view (default: ${MCP_TARGET_NS})
  -h, --help                    Show this help

Environment (.env at repo root supported):
  NAMESPACE, LSD_NAME, INFERENCE_MODEL, VLLM_URL, VLLM_API_TOKEN, VLLM_TLS_VERIFY, WITH_MCP, MCP_TARGET_NS
EOF
}

# Parse flags
while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace)
      NAMESPACE="$2"; shift 2;;
    --lsd-name)
      LSD_NAME="$2"; shift 2;;
    --model)
      INFERENCE_MODEL="$2"; shift 2;;
    --vllm-url)
      VLLM_URL="$2"; shift 2;;
    --vllm-token)
      VLLM_API_TOKEN="$2"; shift 2;;
    --tls-verify)
      VLLM_TLS_VERIFY="$2"; shift 2;;
    --with-mcp)
      WITH_MCP="true"; shift 1;;
    --mcp-target-ns)
      MCP_TARGET_NS="$2"; shift 2;;
    -h|--help)
      print_usage; exit 0;;
    *)
      echo "Unknown option: $1"; print_usage; exit 1;;
  esac
done

if [[ -z "${VLLM_URL}" ]]; then
  echo "Error: --vllm-url is required (or set VLLM_URL in .env)."
  echo
  print_usage
  exit 1
fi

if ! command -v oc >/dev/null 2>&1; then
  echo "Error: 'oc' CLI not found. Install and login first."
  exit 1
fi

echo "Checking Llama Stack CRD presence..."
if ! oc get crd llamastackdistributions.llamastack.io >/dev/null 2>&1; then
  echo "CRD 'llamastackdistributions.llamastack.io' not found."
  echo "Install the Llama Stack Operator/CRDs before proceeding."
  exit 1
fi

echo "Ensuring namespace '${NAMESPACE}' exists..."
if ! oc get namespace "${NAMESPACE}" >/dev/null 2>&1; then
  oc new-project "${NAMESPACE}" || true
fi

if [[ "${WITH_MCP}" == "true" ]]; then
  echo "Deploying Kubernetes MCP Server into '${NAMESPACE}' (target view ns: ${MCP_TARGET_NS})..."
  oc process -f "${TEMPLATE_MCP}" \
    -p DEPLOY_NAMESPACE="${NAMESPACE}" \
    -p TARGET_NAMESPACE="${MCP_TARGET_NS}" \
  | oc apply -f -

  echo "MCP Server rolled out (Service: kubernetes-mcp-server.${NAMESPACE})."
fi

echo "Deploying Llama Stack Distribution '${LSD_NAME}' into '${NAMESPACE}'..."
tmp_json="$(mktemp)"
tmp_filtered="$(mktemp)"
oc process -f "${TEMPLATE_LSD}" -o json \
  -p NAMESPACE="${NAMESPACE}" \
  -p LSD_NAME="${LSD_NAME}" \
  -p INFERENCE_MODEL="${INFERENCE_MODEL}" \
  -p VLLM_URL="${VLLM_URL}" \
  -p VLLM_API_TOKEN="${VLLM_API_TOKEN}" \
  -p VLLM_TLS_VERIFY="${VLLM_TLS_VERIFY}" \
> "${tmp_json}"

# Remove any Project objects from the rendered template (when namespace already exists)
if command -v jq >/dev/null 2>&1; then
  jq '
    if .kind == "List" and (.items|type=="array") then
      .items |= map(select(.kind != "Project"))
    elif .kind == "Project" then
      empty
    else
      .
    end
  ' "${tmp_json}" > "${tmp_filtered}"
else
  python3 - "$tmp_json" "$tmp_filtered" <<'PYCODE'
import json, sys
src, dst = sys.argv[1], sys.argv[2]
with open(src) as f:
    data = json.load(f)
if data.get("kind") == "List" and isinstance(data.get("items"), list):
    data["items"] = [i for i in data["items"] if i.get("kind") != "Project"]
    with open(dst, "w") as out:
        json.dump(data, out)
elif data.get("kind") == "Project":
    # write nothing; caller will skip apply if file is empty
    open(dst, "w").close()
else:
    with open(dst, "w") as out:
        json.dump(data, out)
PYCODE
fi

if [[ -s "${tmp_filtered}" ]]; then
  oc apply -f "${tmp_filtered}"
else
  echo "No resources to apply after filtering."
fi
rm -f "${tmp_json}" "${tmp_filtered}"

echo "Applied resources. Waiting briefly for pods to appear..."
sleep 3

echo "Current status:"
oc get lsd -n "${NAMESPACE}" || true
oc get pods -n "${NAMESPACE}"

echo
echo "Next steps:"
echo "- Follow logs:"
echo "    oc logs -n ${NAMESPACE} -l app.kubernetes.io/part-of=llamastack -f --tail=200 || true"
echo "- Port-forward to use the API locally on port 8321:"
echo "    POD=\$(oc get pods -n ${NAMESPACE} -o name | grep -i \"${LSD_NAME}\" | head -n1)"
echo "    oc port-forward -n ${NAMESPACE} \"\${POD}\" 8321:8321"
echo "    curl http://localhost:8321/"
echo
echo "If you need to change the MCP endpoint or other runtime config, edit the 'lsd-run' ConfigMap:"
echo "    oc -n ${NAMESPACE} get cm lsd-run -o yaml"
echo "and update run.yaml (e.g., 'mcp_endpoint.uri'), then restart the server pod."
echo
echo "Done."


