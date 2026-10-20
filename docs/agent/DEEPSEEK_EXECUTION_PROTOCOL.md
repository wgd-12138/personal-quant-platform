# DeepSeek execution notes

This note summarizes the original staged development workflow. The private task plans, machine-specific evidence, and intermediate work logs are intentionally not included in this public repository.

For a public checkout, use the following review loop:

1. Read `README.md` and `docs/agent/PUBLIC_STATUS.md` before making claims about capabilities.
2. Read the relevant contract under `docs/agent/` and inspect its implementation and tests.
3. Make one bounded change at a time; preserve fail-closed behavior and the no-live-trading boundary.
4. Run `uv run --no-sync --extra agent python -m compileall -q src tests`.
5. Run `uv run --no-sync --extra agent python agent_eval/run_unittest_shim.py` on restricted Windows hosts, then rerun the evaluation and offline demo commands in `docs/agent/EVAL.md`.
6. Report exact commands, results, environment limitations, and any remaining unknowns.

Do not use local fixtures or deterministic placeholder backends as evidence of live market data, semantic retrieval quality, container isolation, or SkyWalking ingestion.
