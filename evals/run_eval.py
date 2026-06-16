"""Eval runner using execution accuracy.

Reads evals/eval_set.jsonl, calls the agent at AGENT_URL on each question,
then compares the agent's SQL output to the gold SQL by *executed rows*
(canonicalized: sorted, stringified, None-coerced to empty).

Helpers (run_sql / canonicalize / matches) are provided. You implement
eval_one() and summarize().

Run:
    uv run python evals/run_eval.py --out results/eval_baseline_cpu.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EVAL_FILE = ROOT / "evals" / "eval_set.jsonl"
DEFAULT_OUT_FILE = ROOT / "results" / "eval_baseline_cpu.json"
DB_DIR = ROOT / "data" / "bird"
AGENT_URL_DEFAULT = "http://localhost:8001/answer"


# ---------- Helpers (provided) -----------------------------------------

def run_sql(db_id: str, sql: str, timeout: float = 5.0) -> tuple[bool, list[tuple] | None, str | None]:
    """Run sql against db_id in read-only mode. Returns (ok, rows, error)."""
    path = DB_DIR / f"{db_id}.sqlite"
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=timeout) as conn:
            cur = conn.execute(sql)
            rows = cur.fetchall()
            return True, rows, None
    except Exception as e:  # noqa: BLE001
        return False, None, f"{type(e).__name__}: {e}"


def canonicalize(rows: list[tuple] | None) -> list[tuple] | None:
    """Sort rows; coerce cells to str; None -> ''."""
    if rows is None:
        return None
    return sorted(tuple("" if c is None else str(c) for c in row) for row in rows)


def matches(gold_rows: list[tuple] | None, pred_rows: list[tuple] | None) -> bool:
    if gold_rows is None or pred_rows is None:
        return False
    return canonicalize(gold_rows) == canonicalize(pred_rows)


# ---------- Implement these (Phase 5) ----------------------------------

def _agent_attempts(history: list[dict], final_sql: str) -> list[str]:
    """Extract the SQL attempts emitted by the agent, in order.

    Step by step:
    1. Walk the agent history returned by `/answer`.
    2. Keep SQL from nodes that create SQL: `generate_sql` and `revise`.
    3. Ignore verifier entries because they do not contain a new query.
    4. If history is missing SQL, fall back to the final SQL response.

    The returned list is what we use for per-iteration scoring.
    """
    attempts = [
        str(item["sql"])
        for item in history
        if item.get("node") in {"generate_sql", "revise"} and item.get("sql")
    ]
    if not attempts and final_sql:
        attempts.append(final_sql)
    return attempts


def _score_sql_attempts(db_id: str, gold_rows: list[tuple] | None, attempts: list[str]) -> list[dict]:
    """Execute and score every SQL attempt against the gold result rows.

    Step by step:
    1. Run each generated SQL query against the same SQLite database.
    2. Compare its result rows with the already-executed gold SQL rows.
    3. Store whether execution succeeded, any error, and whether rows matched.
    4. Return one score record per generate/revise attempt.

    This lets us answer: "Would the agent have been correct after iteration 1,
    iteration 2, etc.?"
    """
    scores: list[dict] = []
    for iteration, sql in enumerate(attempts, start=1):
        pred_ok, pred_rows, pred_error = run_sql(db_id, sql)
        scores.append({
            "iteration": iteration,
            "sql": sql,
            "execution_ok": pred_ok,
            "error": pred_error,
            "row_count": len(pred_rows or []),
            "correct": matches(gold_rows, pred_rows),
        })
    return scores


def eval_one(question: dict, agent_url: str) -> dict:
    """Score one question by comparing executed rows, not SQL text.

    Step by step:
    1. Execute the gold SQL locally to get the expected result rows.
    2. Send the natural-language question and database id to the running agent.
    3. Read the agent's final SQL, rows, iteration count, and history.
    4. Extract every SQL attempt from history: initial generation plus revisions.
    5. Execute each attempt locally and compare rows to the gold rows.
    6. Store the user query, final SQL, final response rows, and raw agent response.
    7. Return a detailed record that can be aggregated later.

    The agent must already be running at `agent_url`.
    """
    db_id = question["db_id"]
    gold_sql = question["gold_sql"]
    gold_ok, gold_rows, gold_error = run_sql(db_id, gold_sql)

    request_payload = {
        "question": question["question"],
        "db": db_id,
        "tags": {
            "run_type": "eval",
            "db_id": db_id,
        },
    }

    try:
        response = httpx.post(agent_url, json=request_payload, timeout=240.0)
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text}")
        agent = response.json()
        agent_error = None
    except Exception as e:  # noqa: BLE001
        agent = {}
        agent_error = f"{type(e).__name__}: {e}"

    final_sql = str(agent.get("sql") or "")
    history = agent.get("history") or []
    attempts = _agent_attempts(history, final_sql)
    iteration_scores = (
        _score_sql_attempts(db_id, gold_rows, attempts)
        if gold_ok and attempts
        else []
    )
    final_correct = iteration_scores[-1]["correct"] if iteration_scores else False

    return {
        "query": question["question"],
        "question": question["question"],
        "db_id": db_id,
        "gold_sql": gold_sql,
        "gold_rows": [list(row) for row in (gold_rows or [])] if gold_ok else None,
        "gold_execution_ok": gold_ok,
        "gold_error": gold_error,
        "agent_http_error": agent_error,
        "agent_ok": bool(agent.get("ok", False)),
        "agent_error": agent.get("error"),
        "final_sql": final_sql,
        "final_response": agent.get("rows"),
        "final_rows": agent.get("rows"),
        "iterations": int(agent.get("iterations") or len(attempts) or 0),
        "final_correct": final_correct,
        "iteration_scores": iteration_scores,
        "history": history,
        "agent_response": agent,
    }


def summarize(results: list[dict]) -> dict:
    """Aggregate overall and per-iteration execution accuracy.

    Step by step:
    1. Count how many questions were evaluated.
    2. Count how many final answers matched the gold executed rows.
    3. Find the largest number of SQL attempts any question made.
    4. For each iteration, use that iteration's score when present.
    5. If a question stopped earlier, carry forward its final score.
    6. Return pass rates plus basic error counts for debugging.

    Per-iteration carry-forward: if the agent terminated at iteration j < k
    (verify said ok at j, or it hit MAX_ITERATIONS at j < k), treat the
    question's iteration-k result as identical to its iteration-j result.
    The agent stopped emitting; whatever it had at termination is what
    would have been served had we polled at iteration k.
    """
    total = len(results)
    final_correct = sum(1 for r in results if r.get("final_correct"))
    final_incorrect = total - final_correct
    agent_http_errors = sum(1 for r in results if r.get("agent_http_error"))
    agent_reported_errors = sum(1 for r in results if r.get("agent_error"))
    agent_success_count = sum(
        1
        for r in results
        if not r.get("agent_http_error") and not r.get("agent_error") and r.get("agent_ok")
    )
    final_execution_success_count = sum(
        1
        for r in results
        if r.get("iteration_scores") and r["iteration_scores"][-1].get("execution_ok")
    )
    revision_count = sum(
        1
        for r in results
        if any(item.get("node") == "revise" for item in r.get("history", []))
    )
    total_iterations = sum(int(r.get("iterations") or 0) for r in results)
    max_iteration = max(
        (len(r.get("iteration_scores", [])) for r in results),
        default=0,
    )

    per_iteration: list[dict] = []
    for iteration in range(1, max_iteration + 1):
        correct = 0
        scored = 0
        for result in results:
            scores = result.get("iteration_scores", [])
            if not scores:
                continue
            idx = min(iteration, len(scores)) - 1
            correct += int(bool(scores[idx]["correct"]))
            scored += 1
        per_iteration.append({
            "iteration": iteration,
            "correct": correct,
            "total": scored,
            "accuracy": correct / scored if scored else 0.0,
        })

    return {
        "total": total,
        "final_correct": final_correct,
        "final_incorrect": final_incorrect,
        "final_accuracy": final_correct / total if total else 0.0,
        "gold_execution_errors": sum(1 for r in results if not r.get("gold_execution_ok")),
        "agent_http_errors": agent_http_errors,
        "agent_reported_errors": agent_reported_errors,
        "agent_success_count": agent_success_count,
        "agent_success_rate": agent_success_count / total if total else 0.0,
        "final_execution_success_count": final_execution_success_count,
        "final_execution_success_rate": final_execution_success_count / total if total else 0.0,
        "average_iterations": total_iterations / total if total else 0.0,
        "revision_count": revision_count,
        "revision_rate": revision_count / total if total else 0.0,
        "max_iteration": max_iteration,
        "per_iteration": per_iteration,
    }


# ---------- Main (provided) --------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--eval-set", type=Path, default=DEFAULT_EVAL_FILE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_FILE)
    parser.add_argument("--agent-url", default=AGENT_URL_DEFAULT)
    args = parser.parse_args()

    questions = [json.loads(line) for line in args.eval_set.read_text().splitlines() if line.strip()]
    print(f"Loaded {len(questions)} eval questions from {args.eval_set}")

    results: list[dict] = []
    t0 = time.monotonic()
    for i, q in enumerate(questions, 1):
        print(f"[{i}/{len(questions)}] {q['db_id']}: {q['question'][:60]}...", flush=True)
        results.append(eval_one(q, args.agent_url))
    elapsed = time.monotonic() - t0

    summary = summarize(results)
    out = {
        "summary": summary,
        "wall_clock_seconds": elapsed,
        "results": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2))
    print(f"Wrote {args.out}")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
