"""LangGraph agent: text-to-SQL with verify+revise loop.

Graph shape:

    START -> attach_schema -> generate_sql -> execute -> verify
                                                          |
                                              ok=true ----+----> END
                                                          |
                                              ok=false ---+----> revise -> execute -> verify (loop)

Loop is capped at MAX_ITERATIONS total generate/revise calls.

The execute node and the graph wiring are provided. `generate_sql_node` is
filled in as a worked example; you implement `verify`, `revise`, and the
conditional router following the same shape.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

from agent import prompts
from agent.execution import ExecutionResult, execute_sql
from agent.schema import render_schema

# Total generate + revise calls before the loop is forced to stop.
# 3-5 is a reasonable range; tune it as part of Phase 3.
MAX_ITERATIONS = 2

VLLM_BASE_URL = os.environ.get("VLLM_BASE_URL", "http://localhost:8000/v1")
VLLM_MODEL = os.environ.get("VLLM_MODEL", "Qwen/Qwen3-30B-A3B-Instruct-2507")
# vLLM ignores the key, but a hosted OpenAI-compatible provider needs a real one.
# Lets you point the agent at e.g. OpenAI while iterating without a running vLLM.
LLM_API_KEY = os.environ.get("OPENAI_API_KEY", "not-needed")


@dataclass
class AgentState:
    """State threaded through the graph. Extend with fields you need."""

    question: str
    db_id: str
    schema: str = ""
    sql: str = ""
    execution: ExecutionResult | None = None
    verify_ok: bool = False
    verify_issue: str = ""
    iteration: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)


def llm() -> ChatOpenAI:
    """Chat client pointed at VLLM_BASE_URL (your local vLLM by default)."""
    return ChatOpenAI(
        model=VLLM_MODEL,
        base_url=VLLM_BASE_URL,
        api_key=LLM_API_KEY,
        temperature=0.0,
    )


# ---- Nodes ------------------------------------------------------------

def _attach_schema(state: AgentState) -> dict:
    """Provided. Render the DB schema once at the start of the run."""
    return {"schema": render_schema(state.db_id)}


def _extract_sql(text: str) -> str:
    """Pull a read-only SQL statement out of an LLM reply."""
    fenced = re.search(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    candidate = (fenced.group(1) if fenced else text).strip()
    candidate = re.sub(r"<think>.*?</think>", "", candidate, flags=re.DOTALL | re.IGNORECASE).strip()
    match = re.search(r"\b(?:WITH|SELECT)\b.*", candidate, re.DOTALL | re.IGNORECASE)
    if match is not None:
        candidate = match.group(0).strip()
    statements = [part.strip() for part in candidate.split(";") if part.strip()]
    if statements:
        candidate = statements[0] + ";"
    return candidate


def _extract_json_object(text: str) -> dict[str, Any]:
    """Parse the first JSON object from an LLM reply."""
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    candidate = (fenced.group(1) if fenced else text).strip()
    candidate = re.sub(
        r"<think>.*?</think>",
        "",
        candidate,
        flags=re.DOTALL | re.IGNORECASE,
    ).strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        match = re.search(r"\{.*?\}", candidate, re.DOTALL)
        if match is None:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("Verifier response was not a JSON object")
    return parsed


def generate_sql_node(state: AgentState) -> dict:
    """Worked example - the other LLM nodes follow this same shape.

    Build messages from the prompts, call the shared llm(), extract the SQL,
    and return only the state fields you changed. `iteration` is bumped here
    (and in revise) so route_after_verify can enforce MAX_ITERATIONS.

    This node is wired and ready; fill in GENERATE_SQL_SYSTEM / GENERATE_SQL_USER
    in prompts.py to make it produce real queries.
    """
    response = llm().invoke([
        ("system", prompts.GENERATE_SQL_SYSTEM),
        ("user", prompts.GENERATE_SQL_USER.format(
            schema=state.schema,
            question=state.question,
        )),
    ])
    sql = _extract_sql(response.content)
    return {
        "sql": sql,
        "iteration": state.iteration + 1,
        "history": state.history + [{"node": "generate_sql", "sql": sql}],
    }


def execute_node(state: AgentState) -> dict:
    """Provided. Runs the SQL and stores the result."""
    return {"execution": execute_sql(state.db_id, state.sql)}


def verify_node(state: AgentState) -> dict:
    """Decide whether state.execution plausibly answers state.question.

    Follow the generate_sql_node pattern: build messages from the VERIFY_*
    prompts, call llm(), parse the reply. Ask the model for a small JSON object
    like {"ok": bool, "issue": str} and parse it defensively - the model may
    wrap it in prose or fences. state.execution.render() gives you a compact
    view of the rows or error to feed into the prompt.

    Return: {"verify_ok": <bool>, "verify_issue": <str>}.
    What counts as "not plausible" is yours to define - see the Phase 3 targets
    in the README.
    """
    # Step 1: turn the SQL execution output into short text for the LLM.
    # If SQL failed, this text contains the error. If it worked, it contains
    # columns, row count, and a small preview of rows.
    execution_result = (
        state.execution.render()
        if state.execution is not None
        else "ERROR: no execution result available"
    )

    # Step 2: ask the verifier LLM whether the SQL result answers the question.
    # The expected reply is JSON, for example: {"ok": false, "issue": "..."}.
    response = llm().invoke([
        ("system", prompts.VERIFY_SYSTEM),
        ("user", prompts.VERIFY_USER.format(
            question=state.question,
            schema=state.schema,
            sql=state.sql,
            execution_result=execution_result,
        )),
    ])

    # Step 3: parse the LLM's JSON. If parsing fails, we mark verification as
    # failed so the graph can try a revision instead of trusting bad output.
    try:
        parsed = _extract_json_object(response.content)
        verify_ok = bool(parsed.get("ok", False))
        verify_issue = str(parsed.get("issue", "")).strip()
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        verify_ok = False
        verify_issue = f"Verifier returned invalid JSON: {type(e).__name__}: {e}"

    if not verify_ok and not verify_issue:
        verify_issue = "Verifier rejected the SQL without an issue."

    # Step 4: return only the state fields this node changed. LangGraph merges
    # these values back into AgentState before the router decides the next step.
    return {
        "verify_ok": verify_ok,
        "verify_issue": verify_issue,
        "history": state.history + [{
            "node": "verify",
            "ok": verify_ok,
            "issue": verify_issue,
        }],
    }


def revise_node(state: AgentState) -> dict:
    """Produce a revised SQL query given state.verify_issue and the prior attempt.

    Same shape as generate_sql_node, but the prompt should include the failing
    SQL, its execution result, and the verifier's complaint so the model can fix
    it. Bump the iteration counter the same way generate_sql_node does so the
    loop terminates.

    Return: {"sql": <str>, "iteration": state.iteration + 1, ...}.
    """
    # Step 1: prepare the previous execution result so the LLM can see what
    # went wrong: SQL error, empty rows, wrong columns, or implausible rows.
    execution_result = (
        state.execution.render()
        if state.execution is not None
        else "ERROR: no execution result available"
    )

    # Step 2: ask the LLM to repair the previous SQL using the verifier issue.
    # The LLM gets the original question, schema, old SQL, execution result,
    # and a short explanation of the problem.
    response = llm().invoke([
        ("system", prompts.REVISE_SYSTEM),
        ("user", prompts.REVISE_USER.format(
            question=state.question,
            schema=state.schema,
            sql=state.sql,
            execution_result=execution_result,
            verify_issue=state.verify_issue,
        )),
    ])

    # Step 3: extract the corrected SQL and save it as the new current SQL.
    # The graph will execute this revised SQL in the next node.
    sql = _extract_sql(response.content)
    return {
        "sql": sql,
        "iteration": state.iteration + 1,
        "history": state.history + [{
            "node": "revise",
            "sql": sql,
            "issue": state.verify_issue,
        }],
    }


def route_after_verify(state: AgentState) -> str:
    """Conditional router: return "revise" to loop, "end" to terminate.

    Two reasons to end: the verifier was happy (state.verify_ok), or you've hit
    the iteration cap (state.iteration >= MAX_ITERATIONS). Otherwise, revise.
    """
    # If the answer looks good, stop and return it to the caller.
    if state.verify_ok:
        return "end"

    # If we already used all allowed generate/revise attempts, stop even though
    # the answer is not perfect. This prevents infinite loops.
    if state.iteration >= MAX_ITERATIONS:
        return "end"

    # Otherwise, send the graph to revise_node so the LLM can fix the SQL.
    return "revise"


# ---- Graph wiring -----------------------------------------------------

def build_graph():
    g = StateGraph(AgentState)
    g.add_node("attach_schema", _attach_schema)
    g.add_node("generate_sql", generate_sql_node)
    g.add_node("execute", execute_node)
    g.add_node("verify", verify_node)
    g.add_node("revise", revise_node)

    g.add_edge(START, "attach_schema")
    g.add_edge("attach_schema", "generate_sql")
    g.add_edge("generate_sql", "execute")
    g.add_edge("execute", "verify")
    g.add_conditional_edges(
        "verify",
        route_after_verify,
        {"revise": "revise", "end": END},
    )
    g.add_edge("revise", "execute")
    return g.compile()


graph = build_graph()
