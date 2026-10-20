# Public verification status

Snapshot date: **2026-10-20 (Asia/Shanghai)**.

This page records fresh checks run from the public export on 2026-10-20.

## Public-export verification

| Gate | Result |
| --- | --- |
| Compile check | **PASS** — `compileall` completed |
| Agent tests | **PASS** — 816 tests, 1 skipped |
| Full suite | **PASS** — 966 tests, 1 skipped |
| Offline demo | **PASS** — 9/9 steps, 0 outbound attempts |
| Four-layer evaluation | **PASS / UNKNOWN** — L0–L2 passed; L3 remains `unknown` |

The full-suite run used the repository's `agent_eval/run_unittest_shim.py` because this Windows host has a restrictive temporary-directory permission behavior. The standalone Agent test command also passed. Re-run the commands below on the target host before relying on these results.

## Capabilities not verified on this host

| Check | Missing evidence |
| --- | --- |
| Docker isolation | A real Docker engine and a container smoke run |
| SkyWalking APM | Collector → SkyWalking OAP → UI ingest and query evidence for traces, metrics, and logs |
| gVisor | Linux host, `runsc`, Docker runtime registration, and an enforced `--runtime runsc` run |
| Real L2 intent model | A configured model endpoint and a measured live evaluation |
| Chinese semantic embeddings | A real embedding backend and a measured dense/hybrid evaluation; current vector/hybrid evaluation uses a deterministic non-semantic embedder |
| Enterprise load | Concurrent, multi-process, and long-running service tests |
| Cloud deployment | A real Linux cloud host and deployment evidence |
| Clean-host reproduction | A clean checkout and standard full-suite run on a second host without the local shim |

## Known engineering risks

These items are not advertised as complete:

1. Node/tool timeout uses a daemon thread; returning a timeout does not forcibly terminate the underlying Python call. Do not treat this as process-level containment for side effects or untrusted code.
2. Local OTLP capture proves serialization only, not that SkyWalking stored and queried the signals.
3. `LocalRuntime` is a constraint layer, not a security boundary. Docker and gVisor runtime isolation still need host-level verification.
4. L2 golden tasks are a regression set observed during development, not a held-out measure of generalization.

## Public reproduction

From a clean checkout:

```bash
uv sync --extra agent --locked
uv run --no-sync --extra agent python -m compileall -q src tests
uv run --no-sync --extra agent python -m unittest discover -s tests/agent -t .
uv run --no-sync --extra agent python agent_eval/run_unittest_shim.py
uv run --no-sync --extra agent python agent_eval/run_all.py --layers l0,l1,l2,l3 --write
uv run --no-sync --extra agent python -m pq_agent demo --json
uv run --no-sync --extra agent python -m pq_agent demo --status
```

If source documents changed, rebuild the RAG index before the demo:

```bash
uv run --no-sync --extra agent python agent_eval/rebuild_rag_index.py
```

The generated reports and local runtime files are ignored and are not part of the repository. `L3=unknown` is expected without the real Collector/SkyWalking stack; it is not a passing APM result.

## Publication boundary

The public repository excludes:

- `planning/runs/`
- `planning/agent/evidence/`
- `agent_eval/results/`
- real reports, positions, logs, screenshots, and webhook receipts
- local collaboration files, internal gate scripts, and machine environment reports
