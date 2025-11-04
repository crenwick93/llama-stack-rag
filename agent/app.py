"""RAG Agent service

This FastAPI service connects to a running Llama Stack instance, creates a
lightweight agent that can use the builtin RAG tool bound to a specific
vector DB, and exposes two endpoints:

- GET /healthz: quick health probe (used by readiness/liveness checks)
- POST /ask: ask a question and get a response from the agent

The service is intentionally simple for educational purposes: clear logs,
minimal configuration, and sensible defaults that match the accompanying
notebook and OpenShift manifests.
"""

import os
import logging
from functools import lru_cache
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from llama_stack_client import LlamaStackClient, Agent
import uuid


# Configure logging: default to INFO; can be overridden via LOG_LEVEL env var
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("rag-agent-service")


class SuppressHealthzFilter(logging.Filter):
    """Drop noisy /healthz access logs from uvicorn.

    OpenShift health probes hit /healthz frequently. Suppressing those
    access lines keeps the logs focused on educational, high-signal events.
    """
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        # Suppress noisy access log lines for health probes
        if " /healthz " in msg or "GET /healthz" in msg:
            return False
        return True


# Reduce noise from uvicorn access logs but keep others
uvicorn_access_logger = logging.getLogger("uvicorn.access")
uvicorn_access_logger.addFilter(SuppressHealthzFilter())


class AskRequest(BaseModel):
    question: str
    session_id: Optional[str] = None


class AskResponse(BaseModel):
    answer: str
    session_id: str
    model: str


def get_env(name: str, default: Optional[str] = None) -> str:
    value = os.getenv(name, default)
    if value is None or value == "":
        raise RuntimeError(f"Required env var not set: {name}")
    return value


@lru_cache(maxsize=1)
def get_client() -> LlamaStackClient:
    """Create a singleton Llama Stack client.

    Uses LLAMA_BASE_URL (defaults to the internal service URL from the
    OpenShift deployment). Trailing slash is trimmed for consistency.
    """
    base_url = os.getenv(
        "LLAMA_BASE_URL",
        "http://lsd-llama-milvus-inline-service.default.svc.cluster.local:8321",
    ).rstrip("/")
    logger.info("Using Llama Stack at %s", base_url)
    return LlamaStackClient(base_url=base_url)


def select_model(client: LlamaStackClient) -> str:
    """Pick an LLM identifier, preferring the vLLM-backed provider if present."""
    # Prefer vLLM provider if available, else first LLM
    models = list(client.models.list())
    preferred = next((m for m in models if m.model_type == "llm" and getattr(m, "provider_id", None) == "vllm-inference"), None)
    if preferred:
        return preferred.identifier
    generic = next((m for m in models if m.model_type == "llm"), None)
    if not generic:
        raise RuntimeError("No LLM models available on Llama Stack")
    return generic.identifier


def resolve_vector_db_id(client: LlamaStackClient, desired_id: str) -> str:
    """Resolve a usable vector DB id.

    If the requested id exists, use it; otherwise fall back to the first
    available one. If listing fails, return the requested id and let the
    server validate it later.
    """
    try:
        logger.info("Resolving vector DB id; requested=%s", desired_id)
        vdbs = list(client.vector_dbs.list())
    except Exception as exc:
        logger.warning("Failed to list vector DBs: %s", exc)
        # Fall back to desired id; may still be valid server-side
        return desired_id

    match = next((v for v in vdbs if getattr(v, "identifier", None) == desired_id), None)
    if match:
        logger.info("Vector DB resolved: %s", match.identifier)
        return match.identifier
    if vdbs:
        logger.warning("Vector DB '%s' not found; falling back to '%s'", desired_id, vdbs[0].identifier)
        return vdbs[0].identifier
    logger.warning("No vector DBs available on server; using desired id: %s", desired_id)
    return desired_id


@lru_cache(maxsize=1)
def get_agent_config() -> tuple[LlamaStackClient, Agent, str, str]:
    """Initialize the agent and cache it with its model and vector DB.

    Returns a tuple of (client, agent, model_id, vector_db_id).
    """
    client = get_client()
    model_id = select_model(client)
    desired_vdb = os.getenv("VECTOR_DB_ID", "confluence")
    vector_db_id = resolve_vector_db_id(client, desired_vdb)

    instructions = (
        "You are a helpful assistant. Use the RAG tool when appropriate and cite source_url(s)."
    )

    agent = Agent(
        client,
        model=model_id,
        instructions=instructions,
        tools=[
            {
                "name": "builtin::rag/knowledge_search",
                "args": {"vector_db_ids": [vector_db_id]},
            }
        ],
    )

    logger.info("Agent initialized: model=%s vector_db_id=%s tools=%s", model_id, vector_db_id, "builtin::rag/knowledge_search")
    return client, agent, model_id, vector_db_id


app = FastAPI(title="RAG Agent Service", version="0.1.0")


@app.get("/healthz")
def healthz() -> dict:
    """Lightweight health endpoint used by readiness/liveness probes."""
    try:
        client, _, model_id, vector_db_id = get_agent_config()
        # Lightweight check: list 1 model if possible
        _ = model_id or select_model(client)
        return {"status": "ok", "model": model_id, "vector_db_id": vector_db_id}
    except Exception as exc:
        logger.exception("Health check failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest) -> AskResponse:
    """Ask the agent a question.

    - Validates input
    - Ensures a session exists (creates one if not provided)
    - Performs a single non-streaming turn and returns the full answer
    """
    if not req.question or not req.question.strip():
        raise HTTPException(status_code=400, detail="question must be a non-empty string")

    try:
        client, agent, model_id, _ = get_agent_config()
        request_id = uuid.uuid4().hex[:8]
        preview = req.question.strip().replace("\n", " ")
        if len(preview) > 120:
            preview = preview[:117] + "..."
        logger.info("ASK start rid=%s model=%s has_session=%s question=\"%s\"", request_id, model_id, bool(req.session_id), preview)
        if req.session_id:
            session_id = req.session_id
        else:
            # Create a session with a generated name and robustly extract the id
            session_name = f"s{uuid.uuid4().hex}"
            created = agent.create_session(session_name=session_name)
            if isinstance(created, str):
                session_id = created
            elif isinstance(created, dict):
                session_id = created.get("id") or created.get("session_id") or created.get("identifier") or str(created)
            else:
                session_id = getattr(created, "id", None) or getattr(created, "session_id", None) or getattr(created, "identifier", None) or str(created)
            logger.info("Session created rid=%s session_name=%s session_id=%s", request_id, session_name, session_id)

        # Single non-streaming turn: return the complete answer
        result = agent.create_turn(
            messages=[{"role": "user", "content": req.question}],
            session_id=session_id,
            stream=False,
        )
        # The SDK typically returns a dict-like result or typed object; try common attributes
        answer = None
        if isinstance(result, dict):
            answer = result.get("message") or result.get("content") or result.get("text")
        if answer is None and hasattr(result, "message"):
            answer = getattr(result, "message")
        if answer is None and hasattr(result, "content"):
            answer = getattr(result, "content")
        if answer is None:
            # As a last resort, stringify
            answer = str(result)
        logger.info("ASK done rid=%s answer_chars=%s", request_id, len(answer) if isinstance(answer, str) else "n/a")

        return AskResponse(answer=answer, session_id=session_id, model=model_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("/ask failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", os.getenv("SERVICE_PORT", "8080")))
    # Local/dev entrypoint. In containers, uvicorn is started via CMD in the Containerfile.
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)


