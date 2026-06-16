"""FastAPI wrapper exposing the agent over HTTP.

Run:
    uv run uvicorn agent.server:app --host 0.0.0.0 --port 8001

The /answer endpoint accepts {question, db, tags?} and returns the
agent's final SQL, the result rows, and per-iteration history.
"""
from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

load_dotenv(override=True)

from agent.graph import AgentState, graph  # noqa: E402

# Langfuse v3/v4 docs use LANGFUSE_BASE_URL; older examples often use
# LANGFUSE_HOST. Keep both populated so the SDK and local assignment config agree.
if os.environ.get("LANGFUSE_HOST") and not os.environ.get("LANGFUSE_BASE_URL"):
    os.environ["LANGFUSE_BASE_URL"] = os.environ["LANGFUSE_HOST"]

_lf_client: Any = None
_lf_callback_handler: Any = None
if os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"):
    from langfuse import get_client
    from langfuse.langchain import CallbackHandler

    _lf_client = get_client()
    _lf_callback_handler = CallbackHandler


app = FastAPI()


class AnswerRequest(BaseModel):
    question: str
    db: str
    tags: dict[str, str] = Field(default_factory=dict)
    user_id: str | None = None
    session_id: str | None = None


class AnswerResponse(BaseModel):
    sql: str
    rows: list[list[Any]] | None
    iterations: int
    ok: bool
    error: str | None = None
    history: list[dict[str, Any]] = Field(default_factory=list)


@app.get("/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "langfuse": "enabled" if _lf_callback_handler is not None else "disabled",
    }


def _langfuse_tags(req: AnswerRequest) -> list[str]:
    tags = ["text-to-sql-agent", f"db:{req.db}"]
    for key, value in sorted(req.tags.items()):
        if value:
            tags.append(f"{key}:{value}")
    return tags


def _graph_config(req: AnswerRequest) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        **req.tags,
        "db_id": req.db,
        "llm_model": os.environ.get("VLLM_MODEL", ""),
        "langfuse_tags": _langfuse_tags(req),
    }
    if req.user_id:
        metadata["langfuse_user_id"] = req.user_id
    if req.session_id:
        metadata["langfuse_session_id"] = req.session_id
    elif req.tags.get("run"):
        metadata["langfuse_session_id"] = req.tags["run"]

    return {
        "callbacks": [_lf_callback_handler()] if _lf_callback_handler is not None else [],
        "run_name": "text-to-sql-agent-answer",
        "tags": ["text-to-sql-agent", "answer"],
        "metadata": metadata,
    }


@app.post("/answer", response_model=AnswerResponse)
def answer(req: AnswerRequest) -> AnswerResponse:
    state = AgentState(question=req.question, db_id=req.db)
    try:
        final = graph.invoke(state, config=_graph_config(req))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")
    finally:
        if _lf_client is not None:
            _lf_client.flush()

    sql = final.get("sql", "")
    iteration = final.get("iteration", 0)
    history = final.get("history", [])
    execution = final.get("execution")

    if execution is None:
        return AnswerResponse(
            sql=sql,
            rows=None,
            iterations=iteration,
            ok=False,
            error="agent produced no execution result",
            history=history,
        )
    if not execution.ok:
        return AnswerResponse(
            sql=sql,
            rows=None,
            iterations=iteration,
            ok=False,
            error=execution.error,
            history=history,
        )

    return AnswerResponse(
        sql=sql,
        rows=[list(r) for r in (execution.rows or [])],
        iterations=iteration,
        ok=True,
        history=history,
    )
