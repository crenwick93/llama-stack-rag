import os
import logging
from functools import lru_cache
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from llama_stack_client import LlamaStackClient, Agent


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("rag-agent-service")


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
    base_url = os.getenv(
        "LLAMA_BASE_URL",
        "http://lsd-llama-milvus-inline-service.default.svc.cluster.local:8321",
    ).rstrip("/")
    logger.info("Using Llama Stack at %s", base_url)
    return LlamaStackClient(base_url=base_url)


def select_model(client: LlamaStackClient) -> str:
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
    try:
        vdbs = list(client.vector_dbs.list())
    except Exception as exc:
        logger.warning("Failed to list vector DBs: %s", exc)
        # Fall back to desired id; may still be valid server-side
        return desired_id

    match = next((v for v in vdbs if getattr(v, "identifier", None) == desired_id), None)
    if match:
        return match.identifier
    if vdbs:
        logger.warning("Vector DB '%s' not found; falling back to '%s'", desired_id, vdbs[0].identifier)
        return vdbs[0].identifier
    logger.warning("No vector DBs available on server; using desired id: %s", desired_id)
    return desired_id


@lru_cache(maxsize=1)
def get_agent_config() -> tuple[LlamaStackClient, Agent, str, str]:
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

    logger.info("Initialized agent with model=%s and vector_db_id=%s", model_id, vector_db_id)
    return client, agent, model_id, vector_db_id


app = FastAPI(title="RAG Agent Service", version="0.1.0")


@app.get("/healthz")
def healthz() -> dict:
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
    if not req.question or not req.question.strip():
        raise HTTPException(status_code=400, detail="question must be a non-empty string")

    try:
        client, agent, model_id, _ = get_agent_config()
        session_id = req.session_id or agent.create_session()

        # Prefer non-streaming for simplicity; fall back to streaming if needed
        try:
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
        except Exception:
            # Streaming fallback
            chunks: list[str] = []
            stream = agent.create_turn(
                messages=[{"role": "user", "content": req.question}],
                session_id=session_id,
                stream=True,
            )
            for event in stream:
                # Collect any textual content fields commonly emitted
                text = None
                if isinstance(event, dict):
                    text = event.get("text") or event.get("delta") or event.get("content")
                else:
                    # Best-effort attribute access
                    text = getattr(event, "text", None) or getattr(event, "delta", None) or getattr(event, "content", None)
                if text:
                    chunks.append(str(text))
            answer = "".join(chunks).strip() or "(no content)"

        return AskResponse(answer=answer, session_id=session_id, model=model_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("/ask failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", os.getenv("SERVICE_PORT", "8080")))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)


