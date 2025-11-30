# Llama Stack RAG on Red Hat OpenShift AI

This project deploys a supported Llama Stack using the Red Hat OpenShift AI operator. It provides:

- A custom `LlamaStackDistribution` CR to create and manage the Llama Stack server via the operator
- A custom runtime configuration delivered via a `ConfigMap` (`lsd-run`) with `run.yaml`
- Example notebooks for RAG, agents, and tool usage against the deployed stack

- Key OpenShift resources live under `openshift/llama-stack/`:

- `openshift/llama-stack/template.yaml`: Single OpenShift Template that creates the Project, Secret, runtime ConfigMap, and LlamaStackDistribution CR.

The `LlamaStackDistribution` expects a Kubernetes `Secret` named `llama-stack-inference-model-secret` for remote vLLM access (URL, token, etc.).

## Prerequisites

- Access to a running Red Hat OpenShift cluster and `oc` CLI installed and logged in
- Red Hat OpenShift AI operator installed and the `LlamaStackDistribution` CRD available
- Permissions to create resources (CRs, ConfigMaps, Secrets) in your target namespace
- Target namespace available (manifests default to `default`; update as needed)
- Values for remote inference (if using vLLM remotely):
  - `VLLM_URL`, `VLLM_API_TOKEN`, `VLLM_TLS_VERIFY` (e.g., `true`/`false`), and `INFERENCE_MODEL`

## Quick start (Template-based)

1) Create a `.env` file with required parameters:

```bash
NAMESPACE=llama-stack-demo
LSD_NAME=lsd-llama-milvus-inline
INFERENCE_MODEL=llama-4-scout-17b-16e-w4a16
VLLM_URL=https://your-vllm-endpoint/v1
VLLM_API_TOKEN=REDACTED
VLLM_TLS_VERIFY=true
```

2) Process and apply the template:

```bash
oc process -f /Users/crenwick/Documents/SSA_resources/llama-stack-rag/openshift/llama-stack/template.yaml \
  --param-file=/Users/crenwick/Documents/SSA_resources/llama-stack-rag/.env | oc apply -f -
```

If your account cannot create projects, first:

```bash
oc new-project "$NAMESPACE"
oc process -f /Users/crenwick/Documents/SSA_resources/llama-stack-rag/openshift/llama-stack/template.yaml \
  --param-file=/Users/crenwick/Documents/SSA_resources/llama-stack-rag/.env | oc apply -f -
```

3) Watch resources come up:

```bash
oc -n "$NAMESPACE" get llamastackdistributions,deploy,svc,pods
```

4) Access the service (port-forward):

```bash
oc -n "$NAMESPACE" get svc
oc -n "$NAMESPACE" port-forward svc/llama-stack 8321:8321
```

5) Test:

```bash
curl -s http://localhost:8321/v1/models | jq
curl -s http://localhost:8321/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "'"$INFERENCE_MODEL"'",
    "messages": [{"role": "user", "content": "Say hello in one sentence."}],
    "temperature": 0.2
  }' | jq
```

You can also test embeddings:

```bash
curl -s http://localhost:8321/v1/embeddings \
  -H "Content-Type: application/json" \
  -d '{
    "model": "sentence-transformers/nomic-ai/nomic-embed-text-v1.5",
    "input": ["Hello world", "Second sentence"]
  }' | jq
```

You can also use the notebooks under `notebooks/` to exercise inference, RAG, and agents once the stack is reachable.

## Customization

- Namespace: Use the `NAMESPACE` param in your `.env`.
- Runtime configuration: Edit `openshift/llama-stack/template.yaml` `lsd-run` `run.yaml` section to switch providers, vector DBs, tools, or telemetry.
- Resources: Adjust CPU/memory limits/requests via `LIMITS_*` and `REQ_*` params in `.env`.
- Inference provider: Update your `.env` and the `inference` provider settings under `lsd-run` if needed.

## Cleanup

```bash
oc -n "$NAMESPACE" delete llamastackdistribution "$LSD_NAME"
oc -n "$NAMESPACE" delete cm lsd-run
oc -n "$NAMESPACE" delete secret llama-stack-inference-model-secret
oc delete project "$NAMESPACE"   # optional
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

## ServiceNow ITSM custom credential type (AAP Controller and EDA)

This repo includes an Ansible playbook to create a ServiceNow ITSM custom Credential Type in Ansible Automation Platform (AAP) Controller using the `infra.aap_configuration` collection.

### Config-as-Code (CAAC) deployment quickstart

Prereqs:
- Ensure these collections are available (via your Execution Environment or local install):
  - infra.aap_configuration
  - ansible.controller
  - ansible.eda
- Export AAP gateway auth (AAP 2.5+ uses a single URL/token for Controller and EDA):

```bash
export AAP_HOSTNAME="https://aap.example.com"
export AAP_TOKEN="REDACTED"
```

Create all CAAC objects (Credential Type, Project, Credentials) for both Controller and EDA:

```bash
ansible-playbook ansible_deployment/caac/apply.yml
```

Notes:
- You can override defaults with:
  - `-e project_branch=<branch>` or `-e project_org=<org>`
- The credential type injectors are defined so placeholders are stored literally in both Controller and EDA.

Reference:
- infra.aap_configuration collection: https://github.com/redhat-cop/infra.aap_configuration/tree/devel/roles/eda_credential_types

Tip:
- If you prefer ansible-playbook with local collections, install them however you like (e.g. ansible-galaxy collection install …). This repo no longer ships a collections/requirements.yml so AAP Controller will not attempt dependency installs during Project sync. Use an EE that includes the collections, or manage them separately.

### EDA access to Controller (token credential)
- The EDA rulebook activation needs access to AAP Controller to launch job templates.
- We create an EDA credential of type “Red Hat Ansible Automation Platform” using your `AAP_HOSTNAME` and `AAP_TOKEN`:
  - Name: AAP Controller
  - Inputs:
    - host: `AAP_HOSTNAME` with `/api/controller/` appended (e.g., `https://<gateway>/api/controller/`)
    - oauth_token: `AAP_TOKEN`
    - verify_ssl: `AAP_VALIDATE_CERTS` (default true)
- Ensure you export `AAP_TOKEN` before running the CAAC playbook. The activation attaches this credential along with the ServiceNow ITSM credential so both ServiceNow and Controller access are available at runtime.

### Custom EDA Decision Environment with ServiceNow collection
- The supported Decision Environment images do not include all collections. For the ServiceNow source, build a custom DE that includes `servicenow.itsm` (and `ansible.eda`).
- A sample definition is included at `ansible_deployment/eda/decision-environment.yml`.

Build and push (Podman):
```bash
podman login registry.redhat.io

# Choose a destination for your image
REG="<your-registry>/<namespace>"
IMAGE="${REG}/eda-de:latest"

ansible-builder build \
  -f ansible_deployment/eda/decision-environment.yml \
  -t "${IMAGE}" \
  --container-runtime podman

podman push "${IMAGE}"
```

After you have built and pushed your Decision Environment image, set it in the CAAC playbook and apply:

1) Edit `ansible_deployment/caac/apply.yml` and set the `de_image` variable under `vars:` to your image:

```yaml
vars:
  # ...
  de_image: "<your-registry>/<namespace>/eda-de:latest"
```

2) Run the CAAC playbook to create/update the EDA Decision Environment and related objects:

```bash
ansible-playbook ansible_deployment/caac/apply.yml
```

Ensure your activation in `ansible_deployment/caac/vars.yml` references the desired Decision Environment name (edit `decision_environment` if needed).

macOS (Apple Silicon) note:
- If you build on an M1/M2 Mac, you must build an amd64 image or EDA will fail with "Exec format error".
- Build for amd64:
  - Podman (single-step):
    ```bash
    ansible-builder build \
      -f ansible_deployment/eda/decision-environment.yml \
      -t "${IMAGE}" \
      --container-runtime podman \
      --extra-build-cli-args="--arch=amd64"
    podman push "${IMAGE}"
    ```
  - Docker (single-step):
    ```bash
    ansible-builder build \
      -f ansible_deployment/eda/decision-environment.yml \
      -t "${IMAGE}" \
      --container-runtime docker \
      --extra-build-cli-args="--platform=linux/amd64"
    docker push "${IMAGE}"
    ```
- Optional: verify image architecture
  ```bash
  podman manifest inspect "${IMAGE}" | grep -nA1 architecture
  ```
### What it creates
- Name: ServiceNow ITSM Credential
- Inputs:
  - instance (string)
  - username (string)
  - password (string, secret: true)
- Injectors (env):
  - SN_HOST='{{instance}}'
  - SN_USERNAME='{{username}}'
  - SN_PASSWORD='{{password}}'

### Prerequisites
- AAP 2.5+ (tested with 2.6)
- Collection installed:

```bash
ansible-galaxy collection install infra.aap_configuration
```

- Auth to AAP provided via environment variables or extra vars:
  - AAP_HOSTNAME (e.g., https://aap.example.com)
  - AAP_TOKEN (OAuth token)
  - AAP_VALIDATE_CERTS (optional, default true)

### Create in AAP Controller (Config-as-Code)
Run the bundled playbook:

```bash
export AAP_HOSTNAME="https://aap.example.com"
export AAP_TOKEN="REDACTED"
# export AAP_VALIDATE_CERTS=false   # if needed

ansible-playbook ansible/caac/servicenow_credential_type.yml
```

This uses the `infra.aap_configuration.dispatch` role and `aap_credential_types` data model to create the custom Credential Type in Controller.

### Event-Driven Ansible (EDA)
For this demo, you also need the same custom Credential Type in the EDA controller:

- Option A (UI): In EDA, navigate to Automation Decisions → Infrastructure → Credential Types and create the same type with the inputs/injectors shown above.

- Option B (Config-as-Code): Extend the playbook to include an EDA block using the same definition (the `infra.aap_configuration` collection includes EDA roles). For example:

```yaml
# Snippet to add under vars: in ansible/caac/servicenow_credential_type.yml
eda_credential_types:
  - name: "ServiceNow ITSM Credential"
    description: "Description of your credential type"
    kind: "cloud"
    inputs:
      fields:
        - id: instance
          type: string
          label: Instance
        - id: username
          type: string
          label: Username
        - id: password
          type: string
          label: Password
          secret: true
      required:
        - instance
        - username
        - password
    injectors:
      env:
        SN_HOST: "{{ instance }}"
        SN_USERNAME: "{{ username }}"
        SN_PASSWORD: "{{ password }}"
```

You can reuse the same platform OAuth token if your setup centralizes API access, otherwise provide credentials appropriate for the EDA API endpoint.

### Reference
- infra.aap_configuration collection: `https://github.com/redhat-cop/infra.aap_configuration`

---

## Special Payment Project: Monitoring and ServiceNow integration

This repo includes a small demo app under `special_project_app/` plus turnkey monitoring that sends alerts to ServiceNow via a lightweight bridge.

What it deploys
- Namespace `special-payment-project` with:
  - `checkout-api` (FastAPI) and `checkout-frontend` (nginx unprivileged)
  - `ServiceMonitor` to scrape `checkout-api` `/metrics`
  - `PrometheusRule` with example alerts:
    - `PaymentUpstream` (based on `special_project_upstream_ok`)
    - `DeploymentUnavailable`, `PodCrashLooping`
  - `AlertmanagerConfig` routing alerts to a ServiceNow bridge
- Namespace `special-monitoring` with:
  - `snow-bridge` Deployment + Service (receives Alertmanager webhooks, calls ServiceNow)
  - Secrets for ServiceNow instance URL and credentials

Files
- App + deploy:
  - `special_project_app/openshift/00-namespace.yaml`
  - `special_project_app/openshift/10-builds-imagestreams.yaml`
  - `special_project_app/openshift/20-deploy-services.yaml`
  - `special_project_app/openshift/30-routes.yaml`
  - `special_project_app/openshift/40-payments-external.yaml`
- Monitoring:
  - `special_project_app/monitoring/servicemonitor.yaml`
  - `special_project_app/monitoring/prometheusrule.yaml`
  - `special_project_app/monitoring/alertmanagerconfig.yaml`
  - `special_project_app/monitoring/servicenow-secret.example.yaml` (example only)
- Bridge:
  - `special_project_app/monitoring/snow-bridge/` (simple webhook → ServiceNow)
  - Built and pushed via ImageStream/BuildConfig; see deploy script below

Prerequisites
- `oc` CLI logged into a cluster
- Permissions:
  - Project-level: create in `special-payment-project` and `special-monitoring`
  - Cluster-admin if you want this script to enable User Workload Monitoring (UWM) for you
- Optional: ServiceNow instance and credentials for incident creation

Configure ServiceNow (recommended)
Create a `.env` in `special_project_app/` if you want the deploy script to create/update Secrets automatically:

```bash
# ServiceNow
SERVICENOW_INSTANCE_URL="https://your-instance.service-now.com"
SERVICENOW_USERNAME="your.user"
SERVICENOW_PASSWORD="your-password"
```

Notes
- The `snow-bridge` uses two Secrets in the `special-monitoring` namespace:
  - `snow-settings` (key `instance_url`)
  - `snow-credentials` (keys `username`, `password`)
- If you do not use `.env`, you can create them manually:

```bash
oc -n special-monitoring create secret generic snow-settings \
  --from-literal=instance_url="https://your-instance.service-now.com"
oc -n special-monitoring create secret generic snow-credentials \
  --from-literal=username="your.user" \
  --from-literal=password="your-password"
```

Enable User Workload Monitoring (if not already enabled)
- The deploy script below will attempt to enable UWM and user-workload Alertmanager using cluster ConfigMaps:
  - `openshift-monitoring/cluster-monitoring-config` with `enableUserWorkload: true`
  - `openshift-user-workload-monitoring/user-workload-monitoring-config` with `alertmanager.enabled: true` and `enableAlertmanagerConfig: true`
- If you do not have cluster-admin, ask an admin to enable these before proceeding.

Deploy app + monitoring
From `special_project_app/`:

```bash
./scripts/oc-deploy.sh <APPS_DOMAIN>
# example:
./scripts/oc-deploy.sh apps.cluster-xxxx.example.com
```

What the script does
- Creates namespaces:
  - `special-payment-project` (labeled `openshift.io/user-monitoring=true`)
  - `special-monitoring`
- Builds and pushes images from local sources (binary builds):
  - `frontend/`, `api/`, and `monitoring/snow-bridge/`
- Applies Deployments/Services and Routes
- Creates `ServiceMonitor`, `PrometheusRule`, and `AlertmanagerConfig`
- If `.env` is present, creates/updates `snow-settings` and `snow-credentials` Secrets
- Enables UWM + user-workload Alertmanager (cluster-admin required)

How alerting is wired
- `ServiceMonitor` scrapes `checkout-api` on `/metrics` (port name `http`)
- `PrometheusRule` fires the example alerts noted above
- `AlertmanagerConfig` in `special-payment-project` sends webhooks to:
  - `http://snow-bridge.special-monitoring.svc.cluster.local:8080/alerts`
  - The `snow-bridge` then authenticates to ServiceNow using the Secrets and creates incidents

Verify
- Open the site:
  - `https://special-payments.<APPS_DOMAIN>`
- Metrics (OpenShift Console → Observe → Metrics):
  - Query: `special_project_upstream_ok` (namespace: `special-payment-project`)
- Alerts (OpenShift Console → Observe → Alerts):
  - Look for `PaymentUpstream`, `DeploymentUnavailable`, `PodCrashLooping`

Trigger an alert (demo)
From `special_project_app/`:

```bash
./scripts/break_dns.sh
# refresh the app and click Pay — expect a 502 and alert `PaymentUpstream`
./scripts/restore_dns.sh
```

Clean up
```bash
oc delete -f special_project_app/openshift/30-routes.yaml || true
oc delete -f special_project_app/openshift/40-payments-external.yaml || true
oc delete -f special_project_app/openshift/20-deploy-services.yaml || true
oc delete -f special_project_app/monitoring/servicemonitor.yaml || true
oc delete -f special_project_app/monitoring/prometheusrule.yaml || true
oc delete -f special_project_app/monitoring/alertmanagerconfig.yaml || true
oc delete project special-payment-project || true
oc delete project special-monitoring || true
```

More details
- See `special_project_app/README.md` for app-specific behavior and DNS failure demo.
