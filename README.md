# Llama Stack RAG on Red Hat OpenShift AI

This project deploys a supported Llama Stack using the Red Hat OpenShift AI operator. It provides:

- A custom `LlamaStackDistribution` CR to create and manage the Llama Stack server via the operator
- A custom runtime configuration delivered via a `ConfigMap` (`lsd-run`) with `run.yaml`
- Example notebooks for RAG, agents, and tool usage against the deployed stack

Key OpenShift manifests live under `openshift/llama-stack/`:

- `openshift/llama-stack/configmap.yaml`: Defines `ConfigMap/lsd-run` containing the Llama Stack `run.yaml` configuration
- `openshift/llama-stack/llamastackdistribution.yaml`: Defines the `LlamaStackDistribution` instance that references `lsd-run` and sets server resources/env

The `LlamaStackDistribution` expects a Kubernetes `Secret` named `llama-stack-inference-model-secret` for remote vLLM access (URL, token, etc.).

## Prerequisites

- Access to a running Red Hat OpenShift cluster and `oc` CLI installed and logged in
- Red Hat OpenShift AI operator installed and the `LlamaStackDistribution` CRD available
- Permissions to create resources (CRs, ConfigMaps, Secrets) in your target namespace
- Target namespace available (manifests default to `default`; update as needed)
- Values for remote inference (if using vLLM remotely):
  - `VLLM_URL`, `VLLM_API_TOKEN`, `VLLM_TLS_VERIFY` (e.g., `true`/`false`), and `INFERENCE_MODEL`

## Quick start

1) Set your namespace (optional if you use `default`):

```bash
export NS=default
```

2) Create the Secret required by the distribution (adjust values to your environment):

```bash
oc -n ${NS} create secret generic llama-stack-inference-model-secret \
  --from-literal=INFERENCE_MODEL="meta-llama/Meta-Llama-3.1-8B-Instruct" \
  --from-literal=VLLM_URL="https://your-vllm-endpoint/v1" \
  --from-literal=VLLM_API_TOKEN="REDACTED" \
  --from-literal=VLLM_TLS_VERIFY="true"
```

3) Apply the custom Llama Stack configuration `ConfigMap`:

```bash
oc apply -n ${NS} -f openshift/llama-stack/configmap.yaml
```

4) Deploy the Llama Stack via the operator using the custom `LlamaStackDistribution`:

```bash
oc apply -n ${NS} -f openshift/llama-stack/llamastackdistribution.yaml
```

5) Watch the resources come up:

```bash
oc get llamastackdistributions -n ${NS}
oc get pods -n ${NS}
oc describe llamastackdistribution lsd-llama-milvus-inline -n ${NS}
```

6) Access the service:

- The server listens on port `8321` as defined in the CR. Depending on your cluster setup:
  - Port-forward:

    ```bash
    oc -n ${NS} get svc
    # Identify the service created for the Llama Stack
    oc -n ${NS} port-forward svc/llama-stack 8321:8321
    ```

  - Or expose via `Route`/ingress if desired (cluster-specific).

7) Test from your machine or from a pod in-cluster. For example, once forwarded:

```bash
curl -v http://localhost:8321/
```

You can also use the notebooks under `notebooks/` to exercise inference, RAG, and agents once the stack is reachable.

## Customization

- Namespace: Update `metadata.namespace` in `configmap.yaml` and `llamastackdistribution.yaml` or apply with `-n <namespace>`.
- Runtime configuration: Edit `openshift/llama-stack/configmap.yaml` (`data.run.yaml`) to switch providers, vector DBs, tools, or telemetry.
- Resources: Adjust CPU/memory limits/requests under `spec.server.containerSpec.resources` in `llamastackdistribution.yaml`.
- Inference provider: If you change from remote vLLM to another provider, update both `run.yaml` and environment variables/Secrets accordingly.

## Cleanup

```bash
oc delete -n ${NS} -f openshift/llama-stack/llamastackdistribution.yaml
oc delete -n ${NS} -f openshift/llama-stack/configmap.yaml
oc delete secret -n ${NS} llama-stack-inference-model-secret
```

---

## RAG Agent service (headless API)

This repo includes a minimal FastAPI service (`agent/app.py`) that connects to the running Llama Stack and exposes a simple `/ask` endpoint that uses the builtin RAG tool bound to the `confluence` vector DB.

### Assumptions

- **Llama Stack is running** using the manifests above (or equivalent) and reachable at the internal service URL used by the Deployment.
- **Vector DB is populated** with your Confluence content and available under id `confluence` (as used in the service). For ingestion, see: [aiops-rag-ingestion](https://github.com/crenwick93/aiops-rag-ingestion).

### Build and push image (macOS → amd64 cluster)

- Podman:

```bash
export NS=default
oc registry login
REGISTRY=$(oc registry info)
IMAGE="${REGISTRY}/${NS}/rag-agent:latest"

podman build --arch amd64 -f agent/Containerfile -t "${IMAGE}" .
podman push "${IMAGE}"
```

- Docker (buildx):

```bash
export NS=default
oc registry login
REGISTRY=$(oc registry info)
IMAGE="${REGISTRY}/${NS}/rag-agent:latest"

# Ensure buildx is enabled: docker buildx create --use
docker buildx build --platform linux/amd64 -f agent/Containerfile -t "${IMAGE}" --push .
```

### Deploy on OpenShift

```bash
oc apply -n ${NS} -f openshift/agent/service.yaml
oc apply -n ${NS} -f openshift/agent/deployment.yaml

# Point Deployment to your pushed image (deployment has a placeholder image)
oc -n ${NS} set image deployment/rag-agent rag-agent="${IMAGE}"

# Optional: override env vars if needed
oc -n ${NS} set env deployment/rag-agent \
  LLAMA_BASE_URL="http://lsd-llama-milvus-inline-service.default.svc.cluster.local:8321" \
  VECTOR_DB_ID="confluence"

oc -n ${NS} rollout status deploy/rag-agent
```

### Test

- Port-forward from your machine:

```bash
oc -n ${NS} port-forward svc/rag-agent 8080:8080 >/dev/null 2>&1 &
sleep 2
curl -s http://localhost:8080/healthz
curl -s -X POST http://localhost:8080/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"Summarise the resolution for disk full on /var"}'
```

- Or create a Route for external access:

```bash
oc apply -n ${NS} -f openshift/agent/route.yaml
HOST=$(oc -n ${NS} get route rag-agent -o jsonpath='{.spec.host}')
curl -s "http://${HOST}/healthz"
```

### Cleanup (agent service)

```bash
oc delete -n ${NS} -f openshift/agent/route.yaml || true
oc delete -n ${NS} -f openshift/agent/deployment.yaml
oc delete -n ${NS} -f openshift/agent/service.yaml
```

For hands-on exploration, see the workbench notebook `notebooks/rag_agent_workbench.ipynb`.