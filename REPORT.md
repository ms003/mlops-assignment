# MLOps Assignment Report

## Phase 1 - vLLM

### Deliverables Checklist

- vLLM is serving `Qwen/Qwen3-30B-A3B-Instruct-2507` at `http://localhost:8000`.
- The OpenAI-compatible API is available at `http://localhost:8000/v1`.
- The model list endpoint is `http://localhost:8000/v1/models`.
- Manual vLLM query evidence is saved at `screenshots/vllm_manual_query.png`.
- The raw manual query response is saved at `results/vllm_manual_query.json`.

### Manual Queries

I sent a manual SQL-generation request directly to vLLM at `http://localhost:8000/v1/chat/completions`.

Question:

```text
What is the coordinates location of the circuits for Australian grand prix?
```

SQL returned by vLLM:

```sql
SELECT c.lat, c.lng
FROM circuits c
JOIN races r ON c.circuitId = r.circuitId
WHERE r.name = 'Australian Grand Prix';
```

This is sensible because it joins `races` to `circuits`, filters for the Australian Grand Prix, and returns latitude and longitude.

The agent health/eval run also made 30 text-to-SQL requests through the same vLLM server. All 30 requests returned executable SQL, and 12 of 30 matched the gold SQL result rows.

### vLLM Start Command

The server is started by `scripts/start_vllm.sh`.

```bash
uv run python -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen3-30B-A3B-Instruct-2507 \
  --host 0.0.0.0 \
  --port 8000 \
  --max-model-len 8192
```

### Config Flags

| Flag | Value | Simple reason |
|---|---:|---|
| `--model` | `Qwen/Qwen3-30B-A3B-Instruct-2507` | This is the required final model from the assignment. |
| `--host` | `0.0.0.0` | This lets the service listen on the VM so port forwarding can reach it. |
| `--port` | `8000` | This matches the README, `.env`, and Prometheus scrape setup. |
| `--max-model-len` | `8192` | The model default context is too large for one H100 KV cache. Our SQL prompts are much smaller, so 8192 is enough and lets the server start. |

### Environment Notes

The first vLLM start failed because the VM did not have Python development headers. vLLM uses Triton/torch compile, and that needs `Python.h`. Installing `python3.12-dev` fixed that.

The script also has a small fallback for `uv`:

```bash
UV_BIN="${UV_BIN:-$(command -v uv || true)}"
if [[ -z "$UV_BIN" && -x "$HOME/.local/bin/uv" ]]; then
    UV_BIN="$HOME/.local/bin/uv"
fi
```

This was needed because `uv` was installed in the user directory at `$HOME/.local/bin/uv`. Some shells can find it, but some non-login scripts cannot because `$HOME/.local/bin` is not always on `PATH`. The fallback simply says: if the shell cannot find `uv`, use the exact location where it is installed.

The script sources `.env` before starting vLLM so the model name and Hugging Face token are available:

```bash
source .env
```

Current `.env` points vLLM clients at:

```text
VLLM_BASE_URL=http://localhost:8000/v1
VLLM_MODEL=Qwen/Qwen3-30B-A3B-Instruct-2507
```

## Phase 3 - Agent

### Deliverables Checklist

- Agent implementation is in `agent/graph.py` and `agent/prompts.py`.
- The `verify -> revise -> execute -> verify` loop is wired in `agent/graph.py`.
- The loop is capped at `MAX_ITERATIONS = 3`.
- Full model eval evidence is saved at `results/eval_qwen3_30b_a3b_instruct_2507.json`.
- Five revision-triggering Task 3 queries are saved at `evals/task3_revised.jsonl`.
- The five-query rerun output is saved at `results/task3_revised.json`.

### Revision Evidence

The full eval run against `Qwen/Qwen3-30B-A3B-Instruct-2507` evaluated 30 questions. It triggered revision on 13 questions, with final accuracy improving from 10/30 at iteration 1 to 12/30 by iteration 2.

The Task 3 revision subset contains five questions selected from the full eval where the agent history included a `revise` node. Rerunning that subset produced `revision_count = 5`, `revision_rate = 1.0`, and no agent HTTP or reported errors. The JSON output includes each request's `history`, including `generate_sql`, `verify`, `revise`, and final `verify` entries.

## Phase 4 - Agent Observability

### Deliverables Checklist

- Langfuse tracing is wired in `agent/server.py` using the Langfuse LangChain callback handler.
- The agent server reports Langfuse status at `http://localhost:8001/health`.
- Trace name is `text-to-sql-agent-answer`.
- Trace tags include `phase:phase4`, `dataset:eval_set_random10`, `model:qwen3_30b_a3b_instruct_2507`, `instrumentation:langfuse_skill_best_practices`, and `run:<run_id>`.
- The verified Phase 4 run id is `phase4_best_practice_random10_1781630321`.
- Langfuse local UI is available at `http://localhost:3001`.
- Required screenshots should be saved as `screenshots/langfuse_trace.png` and `screenshots/langfuse_tags.png`.

### Trace Evidence

After applying Langfuse best practices, I fired 10 random questions from `evals/eval_set.jsonl` through the agent. The local Langfuse API returned 10 traces for session `phase4_best_practice_random10_1781630321`. Three of those requests triggered the `revise` path, so one of them can be used for the waterfall screenshot showing `generate_sql`, `verify`, `revise`, and final `verify` observations.

## Phase 5 - Baseline Evals

### Deliverables Checklist

- Eval runner is implemented in `evals/run_eval.py`.
- Baseline eval output is saved at `results/eval_baseline.json`.
- Grafana screenshot during the eval run should be saved at `screenshots/grafana_eval_run.png`.

### Results

The baseline eval ran all 30 curated questions from `evals/eval_set.jsonl` against the agent. Final execution accuracy was 12/30 = 40%. The agent completed all 30 requests successfully with no HTTP errors and no reported agent errors.

Per-iteration accuracy:

| Iteration | Correct | Accuracy |
|---:|---:|---:|
| 1 | 10/30 | 33.3% |
| 2 | 12/30 | 40.0% |
| 3 | 12/30 | 40.0% |

### Revision Benefit

The revise loop is doing measurable work. Initial SQL generation solved 10 questions, while allowing verification and revision raised the final score to 12. The eval recorded 13 revised questions, so the verifier is actively rejecting plausible-looking but incomplete SQL rather than just accepting the first executable query.

The revisions seem most useful when the first SQL is close but misses a concrete detail that can be described in an actionable verifier issue. Examples include wrong output shape, missing requested identifiers, missing filters, incorrect helper columns, or a query that executes but does not answer every part of the question. The verifier prompt is intentionally strict: it compares the SQL against the question checklist, schema, column dictionaries, output shape, joins, filters, aggregation, ordering, null handling, and encoded values. When it rejects, it names the specific issue so the revise prompt can repair the SQL instead of regenerating blindly.

The revised flow is:

1. `generate_sql` writes an initial SQLite SELECT from the question and schema.
2. `execute` runs it against the target database and returns rows or an error.
3. `verify` asks the model to judge whether the SQL and execution result fully answer the question, returning `{ok, issue}`.
4. If `ok=false`, `revise` receives the original question, schema, previous SQL, execution output, and verifier issue, then writes a corrected SELECT.
5. The graph executes and verifies the revised SQL again, up to `MAX_ITERATIONS = 3` total generate/revise attempts.

The prompt approach that helped was making verification stricter than generation. Generation is asked to produce one valid SQL query. Verification is asked to behave like a semantic judge and reject common false positives: successful SQL with wrong columns, extra helper metrics, missing domain qualifiers, wrong aggregation population, missing null handling, wrong encoded values, or incomplete filters. Revision is then framed as SQL repair, not open-ended generation, and is told to preserve correct parts while fixing the verifier's specific complaint.

### Bottleneck

The main bottleneck is model capability on text-to-SQL semantics, not the mechanics of the loop. Accuracy improves from iteration 1 to iteration 2, but iteration 3 does not add further improvement in this run. That suggests the verifier/reviser can fix some local mistakes, but when the model lacks the correct schema interpretation, join path, encoded value mapping, or aggregation semantics, repeated revision tends to circle around the same wrong interpretation. More iterations add latency and LLM calls without measurable quality gain here, so the current cap of 3 is reasonable. Further improvement likely needs better schema/value grounding, targeted examples for hard BIRD patterns, or stronger model/prompt tuning rather than simply increasing the iteration count.

## Phase 6 - SLO Iteration


### Phase 6 Comparison Table

| Run | Config/change | Requested RPS | Achieved RPS | OK / total | P95 latency | Errors | Pass criteria result |
|---|---|---:|---:|---:|---:|---:|---|
| Original baseline | `--max-model-len 8192` | 5.0 | 4.17 | 250/1500 | 102.3s | timeouts=949, http=1, client=300 | **Fail**: below 10 RPS, P95 far above 5s, many timeouts |
| First fix | `--max-model-len 4096`, `--max-num-seqs 16`, `--max-num-batched-tokens 8192`, prefix caching | 5.0 | 4.63 | 317/1500 | 8.1s | timeouts=2, http=1174, client=7 | **Fail**: latency improved for successful requests, but HTTP 500s dominate |
| Second fix | First fix + `MAX_ITERATIONS = 2` | 5.0 | 4.63 | 315/1500 | 7.0s | timeouts=2, http=1159, client=24 | **Fail**: P95 closer but still >5s, 4096-token context still breaks many requests |

### Baseline Load Test

Baseline file: `results/load_test.json`.

At 5 RPS for 300 seconds, the baseline missed the SLO badly: achieved RPS was 4.17, only 250/1500 requests completed successfully, 949 timed out, and p95 latency was 102.3s. This showed saturation/backlog rather than a small latency regression.

### Diagnosis

The baseline load test failed to meet the SLO because the serving stack became decode-bound under sustained load. Although the driver requested 5 RPS, the system only achieved 4.17 RPS, with p95 latency of 102.3s and 949 request timeouts out of 1500 requests. This is not a small overhead issue in the FastAPI agent or SQLite execution path; it is a serving-capacity problem where in-flight requests accumulate faster than vLLM can finish generating tokens.

The Grafana readout indicated that request admission and prompt processing were not the primary bottleneck: queue and prefill latency stayed comparatively low, and time-to-first-token was around a few seconds rather than the full request duration. The dominant latency came from the token generation/decode phase. Generated-token throughput plateaued around the observed serving ceiling, while request concurrency kept increasing. Once decode throughput flattened, each extra request waited behind ongoing generation work, causing end-to-end request durations to stretch and eventually hit client timeouts.

The agent architecture amplifies this bottleneck because one user request is not one model call. A typical successful run performs `generate_sql` and `verify`, and revised runs add a third LLM call. Therefore 5 agent RPS can translate into roughly 10-15 vLLM calls per second, each with long schema-heavy prompts and short but nonzero completions. Under load, the GPU spends most of its time in decode/inference, not in SQLite execution or HTTP handling.

The proposed fix is to reduce generation demand per agent run and improve vLLM's effective serving capacity. The first tuning attempt targeted KV/cache and batching pressure by reducing `--max-model-len`, bounding active sequences with `--max-num-seqs 16`, bounding batch size with `--max-num-batched-tokens 8192`, and enabling prefix caching. This was directionally correct for latency: p50 improved from 14.4s to 1.2s, p95 improved from 102.3s to 8.1s, and timeouts dropped from 949 to 2. However, `--max-model-len 4096` was too aggressive for the current schema rendering; some prompts exceeded the context limit and produced HTTP 500s.

The practical fix is therefore not simply to keep the 4096-token setting. The next viable fix should preserve the latency gains while avoiding context-limit failures: either restore a larger context window such as 8192, or reduce prompt size with selective schema rendering/retrieval so prompts fit under 4096. In parallel, agent-level changes can reduce decode demand: cap completions more tightly, skip revision when verification is already confident, reduce maximum iterations when load is high, and shorten verifier/reviser prompts. If the SLO still requires 10+ full agent runs per second after those reductions, the remaining solution is more decode capacity: a smaller or quantized model, additional vLLM replicas, or more GPU capacity.

### Iteration 1 - vLLM Context/Concurrency Tuning

Setup files:

- `scripts/start_vllm.sh`
- `results/phase6_tuned_vllm_config.json`
- `results/phase6_tuned_vllm_startup.json`

Change tested:

```text
--max-model-len 4096
--max-num-seqs 16
--max-num-batched-tokens 8192
--gpu-memory-utilization 0.90
--enable-prefix-caching
```

Iteration log:

Saw p95 latency above 100s with many timeouts -> hypothesized that long context/KV pressure and too much concurrency were causing queue buildup -> changed vLLM to shorter max context, conservative sequence concurrency, bounded batched tokens, and prefix caching -> successful-request latency improved substantially, but reliability regressed because some prompts exceeded the 4096-token context limit.

After-run file: `results/load_test_after_tuned_seq16.json`.
Comparison file: `results/phase6_tuned_seq16_comparison.json`.
Error sample: `results/phase6_tuned_seq16_error_sample.json`.

Result at the same 5 RPS target: p50 improved from 14.4s to 1.2s, p95 improved from 102.3s to 8.1s, and timeouts dropped from 949 to 2. However, HTTP 500s increased to 1174 because vLLM rejected prompts over 4096 tokens. A sampled failure showed a `student_club` prompt with 4277 input tokens against a 4096-token context limit.

Verdict: this tuning moved the latency bottleneck in the right direction for successful requests, but `--max-model-len 4096` is not viable for the current schema-rendering approach. The next iteration should either restore a larger context window, reduce schema prompt size, or use selective schema retrieval so the latency gain does not come at the cost of request failures.

### Iteration 2 - Agent Call Reduction

Setup file: `results/phase6_agent_max_iter_2_config.json`.
After-run file: `results/load_test_after_max_iter_2.json`.
Comparison file: `results/phase6_max_iter_2_comparison.json`.
Post-tuning eval: `results/eval_after_tuning.json`.

Change tested:

```text
MAX_ITERATIONS = 2
```

Iteration log:

Saw that iteration 3 did not improve Phase 5 accuracy and that revised requests add extra LLM calls -> hypothesized that removing the third generate/revise attempt would reduce decode demand without losing measured quality -> changed `MAX_ITERATIONS` from 3 to 2 -> successful-request p95 improved again from 8.1s to 7.0s, but the run still failed the SLO because the 4096-token vLLM context limit caused widespread HTTP 500s.

Result at the same 5 RPS target: p50 improved to 1.1s, p95 improved to 7.0s, p99 improved to 8.8s, and timeouts stayed low at 2. However, HTTP errors remained very high at 1159/1500. This confirms that reducing agent calls helps decode latency, but it does not solve the context-limit failure introduced by `--max-model-len 4096`.

The required post-tuning eval was saved to `results/eval_after_tuning.json`. It regressed badly: final accuracy was 2/30, with 25 agent HTTP errors. This is expected from the same context-limit issue and means the current tuned configuration cannot be accepted as the final quality-preserving setup.

### Phase 6 Verdict

The SLO is still missed. The best latency number observed for successful requests after tuning was p95 ~= 7.0s at requested 5 RPS, still above the 5s target and only at half the required 10+ RPS load. More importantly, the current 4096-context configuration is not viable because it fails many requests before quality can be evaluated.

The useful diagnosis is that latency is decode-bound, and reducing wasted LLM calls helps. The practical next fix should keep `MAX_ITERATIONS = 2` because iteration 3 did not improve accuracy, but it should not keep `--max-model-len 4096` unless schema prompts are reduced. A realistic final path is either:

1. restore enough context, likely 8192, while keeping the agent-call reduction and prefix caching; or
2. keep 4096 only after implementing selective schema rendering so every prompt fits; and
3. if 10+ full agent RPS is still required on Qwen3-30B-A3B, add decode capacity through quantization, a smaller acceptable model, or additional vLLM replicas/GPUs.

