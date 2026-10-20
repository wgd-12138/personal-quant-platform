# OpenTelemetry + SkyWalking

This page describes the APM deployment assets and the evidence boundary. The repository contains code and configuration for a three-signal observability path; the complete Collector → SkyWalking OAP → UI flow has **not** been verified on a running stack.

## What is in the repository

| Component | Purpose | Verification status |
| --- | --- | --- |
| OpenTelemetry SDK and exporters | Emit traces, metrics, and logs with allowlisted and sanitized attributes | Unit tests and local OTLP message capture |
| OTel Collector | Receive OTLP, apply memory/batch/redaction processors, and forward signals | Configuration tests only |
| SkyWalking OAP | Store and query telemetry | Not run |
| SkyWalking UI | Inspect traces and service topology | Not run |
| Local APM panel | Display local trace, metric, log, and alert summaries | Local API/UI tests |

Local OTLP capture proves that the application can serialize protocol messages. It does not prove that OAP stored them or that the SkyWalking UI can query them.

## Deployment configuration

The Compose file exposes host ports on `127.0.0.1` only:

| Port | Component | Purpose |
| --- | --- | --- |
| 4317 / 4318 | Collector | OTLP gRPC / HTTP |
| 13133 | Collector | Health check |
| 12800 | SkyWalking OAP | Query API |
| 8080 | SkyWalking UI | Web interface |
| 11800 | SkyWalking OAP | OTLP ingest, internal Compose network |

The three image digests in `deploy/apm/docker-compose.yml` are placeholders. **Replace them with verified registry digests before starting the stack.** The current Compose file is not ready for a real deployment as-is.

After replacing those digests, use:

```bash
docker compose -f deploy/apm/docker-compose.yml config
docker compose -f deploy/apm/docker-compose.yml up -d
docker compose -f deploy/apm/docker-compose.yml ps
```

The `pq_agent` commands are read-only or print-only by default. Adding `--run` to `up` or `down` executes the Compose command:

```bash
uv run --no-sync --extra agent python -m pq_agent status --json
uv run --no-sync --extra agent python -m pq_agent up
uv run --no-sync --extra agent python -m pq_agent up --run
uv run --no-sync --extra agent python -m pq_agent down
```

Stopping the stack preserves its data volumes. `destroy --run` deletes them and should be used only when that data is no longer needed.

## End-to-end acceptance still required

Before describing the APM stack as complete, verify each signal independently:

1. Start Collector, OAP, and UI using verified image digests.
2. Send a real Agent request through the configured OTLP exporter.
3. Confirm Collector received traces, metrics, and logs.
4. Query each signal from SkyWalking and confirm the service topology and trace details.
5. Trigger and resolve an alert; verify both states appear in the platform.
6. Restart the stack and confirm the expected data-retention behavior.

The repository does not currently include a verified end-to-end result for these checks. Keep the APM status `unverified` or `blocked` until then.

## Safety boundaries

- Keep host ports bound to loopback. Use a protected SSH tunnel for remote access.
- The application scrubs telemetry before export; the Collector configuration adds a second filtering boundary.
- Do not send prompts, user data, credentials, webhook URLs, or checkpoint contents as telemetry attributes.
- The local recorder and local UI are development aids, not substitutes for SkyWalking.
- The sample OAP storage uses local H2 storage and is intended for a demo, not a production retention design.

Related code: `src/pq_agent/observability/` and `deploy/apm/`. Relevant tests: `tests/agent/apm/`.
