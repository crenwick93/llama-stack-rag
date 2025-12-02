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

Do NOT include pseudo-tool call syntax (for example lines like [resources_get(...)]
or [pods_log(...)] ) in your final findings output. Only write human-readable
findings and observed values/results. All tool calls must be emitted as real tool
invocations, not printed.

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

def _get_text_from_turn_like_notebook(turn: Any) -> str:
    """
    Extract assistant text similar to the notebook's helper:
    - Prefer output_text
    - Else parse turn.to_dict().output[].content[].text for types output_text/text
    - Else fall back to extract_output_text
    """
    try:
        t = getattr(turn, "output_text", None)
        if isinstance(t, str) and t.strip():
            return t
        if hasattr(turn, "to_dict"):
            d = turn.to_dict()
        elif isinstance(turn, dict):
            d = turn
        else:
            d = None
        if isinstance(d, dict):
            pieces: list[str] = []
            for item in (d.get("output") or []):
                for c in (item.get("content") or []):
                    if isinstance(c, dict) and c.get("type") in ("output_text", "text"):
                        txt = c.get("text", "")
                        if isinstance(txt, str) and txt:
                            pieces.append(txt)
            if pieces:
                return "\n".join(pieces)
            txt2 = d.get("text")
            if isinstance(txt2, str) and txt2.strip():
                return txt2
    except Exception:
        pass
    return extract_output_text(turn)


def build_rag_correlation_instructions() -> str:
    return """
You are a knowledge-base assistant for the Special Payment Project.

INPUTS:
- An incident description.
- A structured summary of cluster findings from diagnostics (pods, logs, Services, DNS, routes).

Goal:
- Correlate findings with the Special Payment Project KB (via file_search) and produce a concise RCA + next steps.
- Prefer project-specific docs over generic Kubernetes docs.

CANONICAL_FALLBACKS (authoritative values to use when the KB is ambiguous or conflicting):
- card-gateway-dns.externalName => card-gateway-sandbox.payments-provider-sim.svc.cluster.local

Matching rules:
- If the KB documents an expected value and the cluster shows a different observed value, explicitly describe the mismatch and treat it as the likely root cause.
- If observed values match the KB, consider other KB-listed causes (backend down, port mismatch, egress policy, TLS expiry, app bugs) guided by the findings.

AAP remediation suggestion:
- Use ONLY job template names exactly as they appear on the “AAP Job Templates” page. Do NOT invent or paraphrase names.
- If the evidence indicates a Kubernetes Service ExternalName mismatch (DNS alias typo/drift), you MUST choose the job template named exactly: "Configure Service".
- Output ONLY job_template_name and extra_vars (as a JSON object of required vars). Do NOT include rollback or additional fields. Do NOT execute anything; suggestion only.

Resolving the canonical FQDN (strict):
- Try to extract the exact canonical FQDN from the KB quotes relevant to card-gateway-dns.
- If multiple candidates appear, choose the one that exactly matches the fallback value.
- If the KB is ambiguous, typo’d, missing, or conflicts, USE the CANONICAL_FALLBACKS value:
  card-gateway-sandbox.payments-provider-sim.svc.cluster.local
- Never output placeholders like "<canonical FQDN from KB>" — always output a concrete FQDN string.

Referencing:
- Do NOT invent document titles or section names.
- When you rely on the KB (expected values, known issues, or job template/vars), include a short quote (1–2 sentences) that could plausibly appear verbatim in the KB. If you used the fallback because the KB was ambiguous, say so briefly.

Reference document (choose ONE that best fits your main evidence):
- "Special Payment Project – Overview & Context"
- "Special Payment Project – Application Architecture"
- "Special Payment Project – Deployment & Configuration"
- "Special Payment Project – Networking & External Dependencies"
- "Special Payment Project – Observability & Alerts"
- "AAP Job Templates"

OUTPUT FORMAT (dual output):
First, produce a concise, human-readable explanation with headings:
- 1) Probable cause — 1–2 lines
- 2) Evidence mapping — bullets quoting observed vs. expected
- 3) Next steps — up to 5 copy/paste commands
- 4) Proposed remediation via AAP — job_template_name + extra_vars (JSON-style; include concrete FQDN)
- 5) Key KB evidence — 1–2 short quotes (or state that fallback was used)
- 6) Reference document — ONE of the whitelisted titles above

Then, on a new line, output a single JSON object (per the schema below) delimited by these exact markers:

### JSON_START
{
  "probable_cause": "string (1–2 sentences)",
  "evidence_mapping": [
    "string — observed finding",
    "string — expected value from KB or canonical fallback",
    "string — explicit mismatch description"
  ],
  "next_steps": [
    { "description": "string", "command": "string" }
  ],
  "proposed_remediation_via_aap": {
    "job_template_name": "string (exact name from 'AAP Job Templates')",
    "extra_vars": {
      "namespace": "string",
      "external_service_name": "string",
      "correct_external_name": "string"   // MUST be a concrete FQDN; never a placeholder
    }
  },
  "key_kb_evidence": [
    "short quote 1 (or 'Using canonical fallback due to ambiguous KB')"
  ],
  "reference_document": "ONE of the whitelisted titles above"
}
### JSON_END

Rules:
- Do not wrap the JSON in markdown fences. Ensure valid JSON (double-quoted keys/strings).
- Use ONLY the fields shown; do not add/rename/remove keys.
- If Service ExternalName mismatch is detected:
  - job_template_name MUST be "Configure Service"
  - extra_vars MUST include:
      namespace: "special-payment-project"
      external_service_name: "card-gateway-dns"
      correct_external_name: (the concrete canonical FQDN derived via KB or CANONICAL_FALLBACKS)
- Never output "<canonical FQDN from KB>" or any placeholder.

FAILSAFE (if evidence/KB is insufficient or retrieval fails):
- You MUST STILL RETURN BOTH the human-readable section AND a valid JSON block where:
  - "probable_cause": "inconclusive"
  - "evidence_mapping": []
  - "next_steps": [
      { "description": "Collect Service spec", "command": "oc get svc -n special-payment-project card-gateway-dns -o yaml" },
      { "description": "Resolve ExternalName from API pod", "command": "oc exec -n special-payment-project deploy/checkout-api -- getent hosts card-gateway-dns" },
      { "description": "Synthetic probe", "command": "curl -i https://special-payments.apps.<APPS_DOMAIN>/api/ping-upstream" }
    ]
  - "proposed_remediation_via_aap": {
      "job_template_name": "",
      "extra_vars": { "namespace": "special-payment-project", "external_service_name": "card-gateway-dns", "correct_external_name": "card-gateway-sandbox.payments-provider-sim.svc.cluster.local" }
    }
  - "key_kb_evidence": ["Using canonical fallback due to missing KB evidence"]
  - "reference_document": "Special Payment Project – Deployment & Configuration"
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
        # Derive an incident question string for prompts (as in the notebook)
        incident_question = ""
        if isinstance(payload, str):
            maybe_q = payload
            if maybe_q.strip():
                incident_question = maybe_q.strip()
        elif isinstance(payload, dict):
            maybe_q = (
                payload.get("incident_question")
                or payload.get("question")
                or payload.get("incident")
                or payload.get("description")
                or payload.get("short_description")
                or ""
            )
            if isinstance(maybe_q, str) and maybe_q.strip():
                incident_question = maybe_q.strip()
        if not incident_question:
            incident_question = "Please investigate the following incident.\n" + summarize_incident_payload(payload)

        client = get_client()
        model_id = select_model(client)
        mcp_url, mcp_label = get_mcp_server()
        mcp_messages = [
            {"role": "system", "content": build_mcp_instructions()},
            {"role": "user", "content": incident_question},
        ]
        mcp_result = client.responses.create(
            model=model_id,
            input=mcp_messages,
            tools=[{"type": "mcp", "server_url": mcp_url, "server_label": mcp_label, "require_approval": "never"}],
            temperature=0.0,
            max_infer_iters=8,
        )
        mcp_findings = extract_output_text(mcp_result).strip()
        # Remove any stray pseudo-tool call lines like [resources_get(...)]
        try:
            import re as _re_mcp
            lines = mcp_findings.splitlines()
            cleaned = []
            for ln in lines:
                if _re_mcp.match(r"^\s*\[[A-Za-z_]+\(", ln) and ln.strip().endswith(")"):
                    # skip pseudo tool-call echo
                    continue
                cleaned.append(ln)
            mcp_findings = "\n".join(cleaned).strip()
        except Exception:
            pass
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
                    "Incident description:\n"
                    + incident_question
                    + "\n\nCluster findings from MCP diagnostics:\n"
                    + (mcp_findings or "(none)")
                ),
            },
        ]
        rag_result = rag_agent.create_turn(messages=rag_messages, session_id=session_id, stream=False)
        # Dual-output extraction (Cell 8 logic)
        raw_text = _get_text_from_turn_like_notebook(rag_result).strip()
        rag_explanation = raw_text
        rag_json = None
        try:
            import re as _re
            m = _re.search(r"### JSON_START\s*(\{.*\})\s*### JSON_END", raw_text, flags=_re.DOTALL)
            if m:
                json_str = m.group(1).strip()
                rag_explanation = raw_text[: m.start()].strip()
                try:
                    rag_json = json.loads(json_str)
                except Exception:
                    rag_json = None
        except Exception:
            pass
    except Exception as exc:
        logger.exception("RAG correlation failed rid=%s: %s", request_id, exc)
        raise HTTPException(status_code=500, detail=f"RAG correlation failed: {exc}")

    # Build combined worknotes (Cell 9-like, plain text)
    worknotes_lines: list[str] = []
    worknotes_lines.append("=" * 80)
    worknotes_lines.append("MCP-First Diagnostics + RAG Correlation (Special Payment Project)")
    worknotes_lines.append("=" * 80)
    worknotes_lines.append("")
    worknotes_lines.append("Phase 1 – MCP diagnostics (live cluster)")
    worknotes_lines.append("-" * 80)
    worknotes_lines.append(mcp_findings if mcp_findings else "(no MCP cluster findings text returned)")
    worknotes_lines.append("")
    worknotes_lines.append("Phase 2 – RAG correlation (knowledge base) — Nicely Formatted")
    worknotes_lines.append("-" * 80)
    worknotes_lines.append(rag_explanation if rag_explanation else "(no formatted RAG text returned)")
    # Ensure a clear reference document line is present (derive from JSON if needed)
    try:
        if rag_json and isinstance(rag_json, dict):
            ref_doc = rag_json.get("reference_document")
            if isinstance(ref_doc, str) and ref_doc.strip():
                worknotes_lines.append("")
                worknotes_lines.append("Reference document")
                worknotes_lines.append("-" * 80)
                worknotes_lines.append(ref_doc.strip())
    except Exception:
        pass
    worknotes_lines.append("")
    worknotes_lines.append("=" * 80)
    worknotes_lines.append("End of diagnostics")
    worknotes_lines.append("=" * 80)
    worknotes = "\n".join(worknotes_lines)

    logger.info("PIPELINE done rid=%s mcp_chars=%s rag_chars=%s", request_id, len(mcp_findings), len(rag_explanation))
    return {
        "session_id": session_id,
        "incident": payload,
        "mcp_findings": mcp_findings,
        "knowledge_base_rag_cross_reference": rag_explanation,
        "worknotes": worknotes,
        "output_as_json": rag_json,
    }


@app.post("/diagnose")
async def diagnose(request: Request) -> dict:
    payload: Any = None
    # Try JSON first; fall back to raw text (ServiceNow description sent as text/plain)
    try:
        payload = await request.json()
    except Exception:
        try:
            raw = await request.body()
            if raw:
                decoded = raw.decode("utf-8", errors="ignore").strip()
                if decoded:
                    payload = decoded
        except Exception:
            pass
    if payload is None:
        raise HTTPException(status_code=400, detail="Request body must be JSON or plain text")
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


