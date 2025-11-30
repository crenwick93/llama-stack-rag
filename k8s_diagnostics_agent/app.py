import os
import json
import uuid
import logging
from functools import lru_cache
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from llama_stack_client import LlamaStackClient, Agent
from pydantic import BaseModel


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("k8s-diagnostics-agent")

# Reduce noisy libraries (httpx/httpcore) and suppress /healthz access logs
for noisy in ("httpx", "httpcore"):
    try:
        logging.getLogger(noisy).setLevel(logging.WARNING)
    except Exception:
        pass

class SuppressHealthzFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "GET /healthz" in msg or " /healthz " in msg:
            return False
        return True

logging.getLogger("uvicorn.access").addFilter(SuppressHealthzFilter())


def _get_env_optional(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name, default)
    if value is None:
        return None
    return value.strip() or default


@lru_cache(maxsize=1)
def get_client() -> LlamaStackClient:
    base_url = (_get_env_optional("LLAMA_BASE_URL") or
                "http://lsd-llama-milvus-inline-service.default.svc.cluster.local:8321").rstrip("/")
    logger.info("Using Llama Stack at %s", base_url)
    return LlamaStackClient(base_url=base_url)


def select_model(client: LlamaStackClient) -> str:
    preferred_id = _get_env_optional("MODEL_ID") or _get_env_optional("PREFERRED_MODEL_ID")
    models = list(client.models.list())
    if preferred_id:
        selected = next((m for m in models if (getattr(m, "identifier", None) or getattr(m, "model_id", None)) == preferred_id), None)
        if selected:
            return getattr(selected, "identifier", None) or getattr(selected, "model_id", None)
        logger.warning("Preferred model %s not found; falling back to auto-select", preferred_id)
    preferred = next((m for m in models if getattr(m, "model_type", None) == "llm" and getattr(m, "provider_id", None) == "vllm-inference"), None)
    if preferred:
        return getattr(preferred, "identifier", None) or getattr(preferred, "model_id", None)
    generic = next((m for m in models if getattr(m, "model_type", None) == "llm"), None)
    if not generic:
        raise RuntimeError("No LLM models available on Llama Stack")
    return getattr(generic, "identifier", None) or getattr(generic, "model_id", None)


@lru_cache(maxsize=1)
def get_vector_store_ids() -> list[str]:
    raw = _get_env_optional("VECTOR_STORE_IDS", "") or ""
    if raw:
        ids = [s.strip() for s in raw.split(",") if s.strip()]
        if ids:
            logger.info("Using VECTOR_STORE_IDS=%s", ids)
            return ids
    single = _get_env_optional("VECTOR_DB_ID", "") or ""
    if single:
        logger.info("Using VECTOR_DB_ID=%s", single)
        return [single]
    return []


def get_mcp_server() -> tuple[str, str]:
    server_url = _get_env_optional("MCP_SERVER_URL") or _get_env_optional("REMOTE_OCP_MCP_URL")
    if not server_url:
        raise RuntimeError("MCP server URL not configured. Set MCP_SERVER_URL or REMOTE_OCP_MCP_URL.")
    server_label = _get_env_optional("MCP_SERVER_LABEL", "kubernetes-mcp") or "kubernetes-mcp"
    return server_url.rstrip("/"), server_label


def extract_output_text(result: Any) -> str:
    try:
        if hasattr(result, "output") and result.output:
            for item in reversed(result.output):
                item_type = getattr(item, "type", None)
                content_list = getattr(item, "content", None)
                if item_type == "message" and content_list:
                    for c in content_list:
                        text = getattr(c, "text", None)
                        if text:
                            return text
        if hasattr(result, "output_text"):
            output_text = getattr(result, "output_text")
            if isinstance(output_text, str) and output_text:
                return output_text
    except Exception:
        pass
    if isinstance(result, dict):
        output = result.get("output")
        if isinstance(output, list):
            for item in reversed(output):
                if item.get("type") == "message":
                    for c in item.get("content", []):
                        text = c.get("text")
                        if text:
                            return text
    return str(result)


@lru_cache(maxsize=1)
def get_rag_agent() -> tuple[LlamaStackClient, Agent, str, list[str]]:
    client = get_client()
    model_id = select_model(client)
    vector_store_ids = get_vector_store_ids()
    rag_instructions = """
You are a knowledge-base assistant for the Special Payment Project.

You ONLY have access to the knowledge base (Confluence docs etc.) via file_search.
You DO NOT have direct access to the live Kubernetes cluster.

You will be given:
- An incident description, and
- A summary of cluster findings from a prior diagnostics pass (pods, logs, services).

Your job:
- Look up relevant information in the knowledge base about the Special Payment Project.
- Try to match the cluster findings to any known issues, incident writeups, or runbooks.
- Explain the most likely root cause(s) in clear language.
- Propose concrete next steps or runbook actions for an SRE.

Ignore generic documentation unless it clearly relates to the given cluster findings.
Be concise, focused, and practical.
""".strip()
    tools: list[dict] = []
    if vector_store_ids:
        tools.append({"type": "file_search", "vector_store_ids": vector_store_ids})
    agent = Agent(client, model=model_id, instructions=rag_instructions, tools=tools)
    return client, agent, model_id, vector_store_ids


def build_mcp_instructions() -> str:
    return """
You are a Kubernetes diagnostics assistant using MCP tools.

You MUST actually call MCP tools to answer the question.
Do NOT simulate tool calls or outputs.
Do NOT write fake examples like [pods_list_in_namespace(...)];
instead, emit real MCP tool calls so the server can execute them.

You do NOT have access to any documentation or knowledge base in this phase.
You MUST NOT guess what the “correct” hostname, port, or configuration should be.
Only report what you can observe directly from MCP tool outputs.

Your focus in the target namespace (for example 'special-payment-project') is to:
- Use pods_list_in_namespace to discover workloads.
- Use pods_log on relevant pods (especially anything in the path of the failing request,
  such as API or frontend pods).
- Use resources_list / resources_get to inspect Services and Deployments.
- Use events_list if you need to check for recent warnings/errors.

When logs show HTTP 5xx or upstream connection errors:
- Identify which upstream hostname or Service is being called (for example from a URL
  like 'http://some-service:port').
- Fetch the Service definition for that upstream using resources_get.
- If the Service is of type ExternalName, include in your findings both:
  - the Service name, and
  - the exact value of spec.externalName as returned by the MCP tool.

In your findings output, you MUST:
- Quote key log lines that look suspicious (5xx, DNS errors, timeouts, TLS failures, etc.).
- List the pods and Services that are clearly in the request path.
- For any ExternalName Services you inspected, make sure the actual externalName value
  appears verbatim somewhere in your summary, so it can be compared later.

If a value looks unusual (for example something that looks like a typo), you may say that it
\"appears suspicious or possibly misconfigured\", but you MUST NOT invent or state the exact
value it “should” be. The exact expected value will be determined in a later knowledge-base
phase.

Your output should be a concise "cluster findings" narrative that highlights:
- Which pods/services are involved in the path of the failing request.
- Key log lines and observed configuration values that look suspicious.
- Any obvious misconfigurations you can see (wrong ports, bad selectors, odd ExternalName, etc.),
  always quoting the concrete values you observed.

Do NOT try to guess business impact or historical context here.
Simply describe what looks wrong or suspicious in the live cluster.
""".strip()


def build_rag_correlation_instructions() -> str:
    return """
You are a knowledge-base assistant for the Special Payment Project.

You are given:
- An incident description.
- A structured summary of cluster findings from a diagnostics pass that already
  inspected pods, logs, and services.

The cluster findings may include:
- Concrete configuration values (for example hostnames, ports, externalName targets,
  Service types, selectors, URLs).
- Log snippets showing HTTP 5xx, connection errors, DNS failures, TLS issues, etc.
- Short notes about which pods and Services appear to sit in the request path.

You have access to a set of Special Payment Project documents stored in a knowledge base
(e.g. exported from Confluence). Their titles and section headings may change over time.

Using ONLY the knowledge base (via file_search), you MUST:
- Look for issues, configuration notes, or design sections that match these findings.
- Pay particular attention to:
  - Expected configuration values (for example, expected hostnames, ports, URL patterns,
    or Service types).
  - Error patterns that resemble the logs in the findings (HTTP 5xx, DNS errors,
    timeouts, TLS failures, etc.).
- Prefer project-specific documentation about the Special Payment Project over generic
  Kubernetes documentation when both are available.

When the KB documents an expected configuration value and the cluster findings show
a different observed value, you MUST:
- Explicitly describe the mismatch in your own words (for example: “the Service in the
  cluster points to X, but the documentation says it should point to Y”).
- Treat such a mismatch as strong evidence of a misconfiguration.
- Clearly state that this configuration mismatch is the most likely root cause in this
  situation, rather than just listing generic “possible causes”.

If the observed values in the cluster match what the KB describes as expected, you should:
- NOT blame a configuration typo by default.
- Instead, consider other causes mentioned in the KB (for example: backend service down,
  wrong port open, network policy restrictions, TLS expiry, application bugs) based on
  the cluster findings.

EVIDENCE AND REFERENCING:
- Do NOT invent document titles or section names.
- Do NOT include pseudo-tool calls like [knowledge_search(...)] or [file_search(...)]
  in your final answer. Just describe what you found in natural language.
- When you rely on the KB for an expected value, configuration detail, or known issue,
  include a short quote (1–2 sentences) from the KB that supports your conclusion.
  The quote MUST be something that could plausibly appear verbatim in the KB.
- Prefer quoting project-specific content (for example, descriptions of Special Payment
  Project services, namespaces, or hostnames) over generic Kubernetes descriptions.

REFERENCE DOCUMENT (WHITELISTED TITLES ONLY):
- At the end of your answer, you MUST add a small reference section in this format:

  Key KB evidence:
  - "<short quote from the KB that supports your conclusion>"
  - (optionally up to 2 more bullets if they are crucial)

  Reference document:
  - "<ONE title from the allowed list below>"

- The Reference document line MUST be exactly one of the following strings:
  - "Special Payment Project – Overview & Context"
  - "Special Payment Project – Application Architecture"
  - "Special Payment Project – Deployment & Configuration"
  - "Special Payment Project – Networking & External Dependencies"
  - "Special Payment Project – Observability & Alerts"

- You MUST NOT write any other value for Reference document.
- You MUST NOT invent new titles or paraphrase these titles.
- If you are uncertain which document the evidence came from, choose the one that
  best matches the content based on its title. For DNS and ExternalName behaviour,
  prefer "Special Payment Project – Networking & External Dependencies".

In all cases:
- Explain the most likely root cause(s) in this specific scenario, grounded in both
  the cluster findings and the documentation.
- Explicitly reference which observed values you are comparing against which expected
  values from the KB (for example: “externalName in the cluster is X, the KB says it
  should be Y”).
- Suggest concrete next steps for an SRE (for example, config changes, rollbacks,
  additional checks to perform).

If the KB is inconclusive, say so, mention what kind of information you looked for,
and suggest what a human should investigate next.
Be concise and practical.
""".strip()


def summarize_incident_payload(payload: Any) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)[:4000]
    except Exception:
        return str(payload)[:4000]


app = FastAPI(title="K8s Diagnostics Agent (MCP + RAG)", version="0.1.0")


@app.get("/healthz")
def healthz() -> dict:
    try:
        # Use cached agent to avoid repeated model listing
        _, _, model_id, vector_store_ids = get_rag_agent()
        mcp_url, _ = get_mcp_server()
        return {"status": "ok", "model": model_id, "vector_store_ids": vector_store_ids, "mcp_server_url": mcp_url}
    except Exception as exc:
        logger.exception("Health check failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


def _run_pipeline(payload: Any) -> dict:
    request_id = uuid.uuid4().hex[:8]
    logger.info("PIPELINE start rid=%s", request_id)

    try:
        client = get_client()
        model_id = select_model(client)
        mcp_url, mcp_label = get_mcp_server()
        mcp_messages = [
            {"role": "system", "content": build_mcp_instructions()},
            {"role": "user", "content": "Incident details (JSON):\n" + summarize_incident_payload(payload)},
        ]
        mcp_result = client.responses.create(
            model=model_id,
            input=mcp_messages,
            tools=[{"type": "mcp", "server_url": mcp_url, "server_label": mcp_label, "require_approval": "never"}],
            temperature=0.0,
            max_infer_iters=8,
        )
        mcp_findings = extract_output_text(mcp_result).strip()
    except Exception as exc:
        logger.exception("MCP diagnostics failed rid=%s: %s", request_id, exc)
        raise HTTPException(status_code=500, detail=f"MCP diagnostics failed: {exc}")

    try:
        _, rag_agent, _, _ = get_rag_agent()
        session = rag_agent.create_session(session_name=f"k8s-diag-{uuid.uuid4().hex[:6]}")
        session_id = (
            getattr(session, "id", None)
            or getattr(session, "session_id", None)
            or getattr(session, "identifier", None)
            or str(session)
        )
        rag_messages = [
            {"role": "system", "content": build_rag_correlation_instructions()},
            {
                "role": "user",
                "content": (
                    "Incident details (JSON):\n"
                    + summarize_incident_payload(payload)
                    + "\n\nCluster findings from MCP diagnostics:\n"
                    + (mcp_findings or "(none)")
                ),
            },
        ]
        rag_result = rag_agent.create_turn(messages=rag_messages, session_id=session_id, stream=False)
        rag_explanation = extract_output_text(rag_result).strip()
    except Exception as exc:
        logger.exception("RAG correlation failed rid=%s: %s", request_id, exc)
        raise HTTPException(status_code=500, detail=f"RAG correlation failed: {exc}")

    logger.info("PIPELINE done rid=%s mcp_chars=%s rag_chars=%s", request_id, len(mcp_findings), len(rag_explanation))
    return {
        "session_id": session_id,
        "incident": payload,
        "mcp_findings": mcp_findings,
        "knowledge_base_rag_cross_reference": rag_explanation,
    }


@app.post("/diagnose")
async def diagnose(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {exc}")
    if payload is None:
        raise HTTPException(status_code=400, detail="Request body must be JSON")
    client = get_client()
    model_id = select_model(client)
    results = _run_pipeline(payload)
    return {"model": model_id, **results}


class AskRequest(BaseModel):
    question: str
    session_id: Optional[str] = None


class AskResponse(BaseModel):
    answer: str
    session_id: str
    model: str


@app.post("/ask", response_model=AskResponse)
async def ask(req: AskRequest) -> AskResponse:
    if not req.question or not req.question.strip():
        raise HTTPException(status_code=400, detail="question must be a non-empty string")
    try:
        client, rag_agent, model_id, _ = get_rag_agent()
        if req.session_id:
            session_id = req.session_id
        else:
            created = rag_agent.create_session(session_name=f"ask-{uuid.uuid4().hex[:6]}")
            session_id = (
                getattr(created, "id", None)
                or getattr(created, "session_id", None)
                or getattr(created, "identifier", None)
                or str(created)
            )
        result = rag_agent.create_turn(messages=[{"role": "user", "content": req.question}], session_id=session_id, stream=False)
        answer = extract_output_text(result).strip()
        return AskResponse(answer=answer, session_id=session_id, model=model_id)
    except Exception as exc:
        logger.exception("/ask failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", os.getenv("SERVICE_PORT", "8080")))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)


