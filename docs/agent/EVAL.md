# Agent evaluation

This evaluation suite checks whether the Agent keeps its safety and behavior contracts after a change. Each layer uses deterministic inputs and explicit thresholds where a local measurement is meaningful. Missing infrastructure remains `unknown` or blocked; it is never replaced with a mock pass.

## Layers

| Layer | What it checks | Current boundary |
| --- | --- | --- |
| L0 | Core and Agent unit tests | Local regression coverage |
| L1 | Intent routing, RAG retrieval, sandbox static checks, and tool schemas | BM25 is the only semantic-independent retrieval backend with a quality threshold |
| L2 | Ten golden-task replays and deterministic assertions | Regression set observed during development, not a held-out generalization set |
| L3 | APM signals, latency, cost, and dropped telemetry | `unknown` until a real Collector/SkyWalking stack stores and serves the signals |

The vector and hybrid retrieval paths currently use a deterministic, non-semantic embedder to exercise channel plumbing. Their scores are not evidence of Chinese semantic retrieval quality. Real L2 intent-model evaluation also requires a configured model endpoint and credentials.

## Reproduce

Install the optional Agent dependencies and refresh the local RAG index if source documents changed:

```bash
uv sync --extra agent --locked
uv run --no-sync --extra agent python agent_eval/rebuild_rag_index.py
```

Run compilation, tests, and all evaluation layers:

```bash
uv run --no-sync --extra agent python -m compileall -q src tests
uv run --no-sync --extra agent python -m unittest discover -s tests/agent -t .
uv run --no-sync --extra agent python agent_eval/run_unittest_shim.py
uv run --no-sync --extra agent python agent_eval/run_all.py --layers l0,l1,l2,l3 --write
uv run --no-sync --extra agent python -m pq_agent demo --json
```

The test shim is for restricted Windows environments where Python's `tempfile` directory permissions prevent the test process from accessing its own temporary directories. It changes only the test process behavior; it does not alter application code. On other hosts, use the standard unittest command.

## Public-export snapshot

Checks rerun from the public export on **2026-10-20**:

| Check | Result |
| --- | --- |
| Compile check | Passed |
| Agent tests | 816 tests, 1 skipped |
| Full suite | 966 tests, 1 skipped |
| Offline demo | 9/9 steps; zero outbound attempts |
| L0–L2 evaluation | Passed |
| L3 APM | `unknown`; no local Docker/SkyWalking stack |
| Intent held-out split | 50 samples; pipeline accuracy 0.9600 |
| Golden tasks | 10/10 |

Generated evaluation reports are written under `agent_eval/results/` and ignored by Git. The public status page summarizes the current verification boundary without publishing machine-specific result files.

## Interpretation

- L3 `unknown` means the platform evidence is missing; it does not mean APM passed.
- Deterministic vector/hybrid scores prove only that the evaluation path runs; they do not measure semantic relevance.
- Static sandbox checks do not prove process isolation. `LocalRuntime` is a constraint layer.
- A 10/10 golden-task result demonstrates regression behavior on those cases, not generalization to unseen requests.
- The offline demo verifies local replay behavior and zero outbound attempts; it does not verify live integrations.

The four-layer runner is `agent_eval/run_all.py`, deterministic assertions are in `agent_eval/judge.py`, and the demo steps are documented in [DEMO_RUNBOOK.md](DEMO_RUNBOOK.md).
