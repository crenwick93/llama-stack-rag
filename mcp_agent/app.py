"""MCP Agent service

This FastAPI service connects to a running Llama Stack instance and creates a
lightweight agent that can use an MCP tool group (e.g., mcp::kubernetes).
It exposes two endpoints:

- GET /healthz: quick health probe
- POST /ask: ask a question and get a response from the agent
"""

import os
import logging
from functools import lru_cache
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from llama_stack_client import LlamaStackClient, Agent
import uuid


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("mcp-agent-service")


class SuppressHealthzFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if " /healthz " in msg or "GET /healthz" in msg:
            return False
        return True


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
    """Create a singleton Llama Stack client."""
    base_url = os.getenv(
        "LLAMA_BASE_URL",
        "http://lsd-llama-milvus-inline-service.default.svc.cluster.local:8321",
    ).rstrip("/")
    logger.info("Using Llama Stack at %s", base_url)
    return LlamaStackClient(base_url=base_url)


def select_model(client: LlamaStackClient) -> str:
    """Pick an LLM identifier, preferring vLLM-backed provider if present."""
    models = list(client.models.list())
    preferred = next((m for m in models if m.model_type == "llm" and getattr(m, "provider_id", None) == "vllm-inference"), None)
    if preferred:
        return preferred.identifier
    generic = next((m for m in models if m.model_type == "llm"), None)
    if not generic:
        raise RuntimeError("No LLM models available on Llama Stack")
    return generic.identifier


@lru_cache(maxsize=1)
def get_agent_config() -> tuple[LlamaStackClient, Agent, str]:
    """Initialize the MCP-enabled agent and cache it with its model."""
    client = get_client()
    model_id = select_model(client)

    # Configure MCP connectivity
    # Prefer explicit MCP server URL/label if provided; otherwise fall back to tool group id for future compatibility.
    mcp_server_url = os.getenv(
        "MCP_SERVER_URL",
        "http://kubernetes-mcp-server.llama-stack-demo.svc.cluster.local:8080/sse",
    )
    mcp_server_label = os.getenv("MCP_SERVER_LABEL", "kubernetes")
    mcp_toolgroup = os.getenv("MCP_TOOLGROUP_ID", "mcp::kubernetes")

    instructions = (
        "You are a helpful assistant. Use the MCP tools when appropriate and be explicit about actions taken."
    )

    # Build tools spec compatible with current client: explicit 'mcp' tool with server details
    tools_spec: list[dict] = [{
        "type": "mcp",
        "mcp": {
            "server_label": mcp_server_label,
            "server_url": mcp_server_url,
        },
    }]
    agent = Agent(
        client,
        model=model_id,
        instructions=instructions,
        tools=tools_spec,
    )

    logger.info("MCP Agent initialized: model=%s tools=%s", model_id, mcp_toolgroup)
    return client, agent, model_id


def extract_answer_text(result) -> str:
    """Extract plain text from a Llama Stack Responses API result."""
    try:
        if hasattr(result, "output") and result.output:
            for item in reversed(result.output):
                item_type = getattr(item, "type", None)
                content_list = getattr(item, "content", None)
                if item_type == "message" and content_list:
                    for content in content_list:
                        text = getattr(content, "text", None)
                        if text:
                            return text
        if hasattr(result, "output_text"):
            output_text = getattr(result, "output_text")
            if isinstance(output_text, str) and output_text:
                return output_text
    except Exception:
        pass
    if isinstance(result, dict):
        choices = result.get("choices")
        if choices:
            msg = choices[0].get("message", {})
            if isinstance(msg, dict):
                text = msg.get("content")
                if text:
                    return text
        output = result.get("output")
        if isinstance(output, list):
            for item in reversed(output):
                if item.get("type") == "message":
                    for c in item.get("content", []):
                        text = c.get("text")
                        if text:
                            return text
    return str(result)


app = FastAPI(title="MCP Agent Service", version="0.1.0")


@app.get("/healthz")
def healthz() -> dict:
    try:
        client, _, model_id = get_agent_config()
        _ = model_id or select_model(client)
        return {"status": "ok", "model": model_id}
    except Exception as exc:
        logger.exception("Health check failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest) -> AskResponse:
    if not req.question or not req.question.strip():
        raise HTTPException(status_code=400, detail="question must be a non-empty string")

    try:
        client, agent, model_id = get_agent_config()
        request_id = uuid.uuid4().hex[:8]
        preview = req.question.strip().replace("\n", " ")
        if len(preview) > 120:
            preview = preview[:117] + "..."
        logger.info("ASK start rid=%s model=%s has_session=%s question=\"%s\"", request_id, model_id, bool(req.session_id), preview)

        if req.session_id:
            session_id = req.session_id
        else:
            session_name = f"s{uuid.uuid4().hex}"
            created = agent.create_session(session_name=session_name)
            if isinstance(created, str):
                session_id = created
            elif isinstance(created, dict):
                session_id = created.get("id") or created.get("session_id") or created.get("identifier") or str(created)
            else:
                session_id = getattr(created, "id", None) or getattr(created, "session_id", None) or getattr(created, "identifier", None) or str(created)
            logger.info("Session created rid=%s session_name=%s session_id=%s", request_id, session_name, session_id)

        result = agent.create_turn(
            messages=[{"role": "user", "content": req.question}],
            session_id=session_id,
            stream=False,
        )
        answer = extract_answer_text(result)
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
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)


