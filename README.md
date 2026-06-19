# Soy Backend

FastAPI mission-orchestration backend for the Piperoni stack.

The unit of work is a **mission** -- typically ingested from a GitHub issue with
the configured run label (`soy-run`). Soy walks each mission through an 8-state
lifecycle using a small agent team (orchestrator / coder / qa / reviewer) backed
by the PraisonAI runtime. Tasks are dispatched to agents, retried up to 3 times,
and escalated after repeated failures or rejections.

---

## Package structure

**Core** -- FastAPI app entrypoint, SQLAlchemy engine/factory, env-driven config
(all values read at call time, never import-time cached), structured JSON logging
to stdout, and structured error envelopes (`{code, detail, ...}` on every 4xx/5xx).

**Models** -- SQLAlchemy ORM layer (DeclarativeBase + TimestampMixin + Uuid
TypeDecorator for dual PG/SQLite support). Six tables forming a hierarchy:

- Mission (top-level orchestration record)
- Agent (one per role within a mission, carries sandbox flag and LLM config)
- Task (unit of work assigned to an agent; dependencies form a DAG)
- Execution (single attempt of a task; retry history via monotonically-increasing attempt_number)
- Approval (human gate decision, one of `planning` or `merge`)
- ChatMessage (PM-Chat lines, sender_type scoped)

All status/role columns are Python enums used by both the ORM and Pydantic
schemas. Migrations are managed by Alembic with explicit hand-written DDL.

**API v1** -- REST routers mounted at `/api/v1` covering missions, agents,
tasks, executions, logs, and webhooks. Plus a WebSocket endpoint at
`/ws/missions/{id}/events` for real-time event push.

**Services** -- Domain layer that bridges the API and external runtimes.
Contains the PraisonAI worker (agent construction, model routing, 3-try
retry, parallel task execution), coding-agent subprocess dispatch,
Git-as-SSOT operations, Mission Control sync, DeerFlow sandbox trigger,
and model resolution (cloud vs local Ollama).

**WebSocket bus** -- Thread-safe in-memory event registry with bounded per-client
queues. The PraisonAI worker publishes from background threads; the WebSocket
coroutine drains each queue and pushes JSON frames to connected clients.

**Tests** -- Pytest suite that runs against an in-memory SQLite database by
default. Fixtures reset the cached engine and worker singleton between tests.

---

## How components interact

The API layer owns HTTP; the services layer owns side effects. The DB is the
single source of truth -- every component reads and writes through SQLAlchemy
sessions, and the WebSocket bus is a thin fan-out layer on top.

```
                    GitHub issue (soy-run label)
                           |
                           v
  Webhook endpoint -----> API Router (missions) -----> DB (missions row)
         |                      |                          |
         |  (Git-as-SSOT)       |  (state transition)      |
         v                      v                          |
  GitService               StateMachine                    |
  (branch, spec.md)        (validates edge)                |
         |                      |                          |
         +-----> writes ------- +------> updates --------- +
                                 |
                                 v
                        PraisonAI Worker
                        /       |       \
                 model       agent      task
                resolver    builder    executor
                   |           |          |
                   v           v          v
               Ollama /    praisonai   ThreadPoolExecutor
               Cloud API   agents     (parallel tasks)
                               |
                               v
                        Event Bus (ws/events)
                               |
                               v
                        WebSocket clients
                        (Mission Control dashboard)
```

Key interactions:

- **API -> StateMachine -> DB**: Every transition call validates the edge
  against the state machine, then writes the new status in the same
  transaction. Row-level locks prevent concurrent transitions.
- **API -> Worker -> DB**: Task execution goes through the worker, which
  opens its own DB session, constructs a PraisonAI agent (with model from
  the resolver and tools from the sandbox flag), runs the task, writes an
  execution row, and updates the task status. On failure, it retries
  (up to 3 attempts) or escalates.
- **Worker -> Event Bus -> WebSocket**: The worker publishes events
  (task.completed, task.escalated, mission.escalated, etc.) to the
  in-memory bus after every state change. The bus fans them out to all
  WebSocket clients subscribed to that mission.
- **Worker -> Mission Control**: After each state change, the API calls
  the MC sync helper (gated, best-effort, 2s timeout). A slow or down MC
  is silently logged and never blocks the request.
- **Worker -> DeerFlow**: When a sandboxed agent's task runs, the worker
  optionally delegates to DeerFlow instead of running tools locally
  (gated by `SOY_DEERFLOW_ENABLED` and the agent's sandbox flag).
- **Worker -> Coding agents**: External CLI agents (OpenCode, Hermes,
  etc.) are invoked as subprocesses via JSON manifests. The dispatcher
  captures stdout/stderr and writes a structured result onto the
  execution row.

---

## How it flows

### Mission lifecycle

A mission moves through 8 states. The state machine is a pure-Python data
structure that the API consults before writing; the DB also enforces the
allowed values via CHECK constraints.

```
created --> planning --> approved --> execution --> reviewed --> merged
              |    |                          |                  |
              |    +--> rejected              +--> rejected      +--> rejected
              |           |
              |           +--(4th rejection)--> escalated
              |
              +--> execution --> escalated
```

- `created -> planning` triggers the PraisonAI planning phase.
- `-> execution` is gated on a `planning_complete` marker (set by a human approval).
- `-> merged` is gated on at least one merge-gate approval; the PR is squash-merged via Git-as-SSOT.
- Rejection sends the mission back to planning. The 4th rejection forces `-> escalated`.
- Concurrent transitions for the same mission are serialized via row-level locking.

### Creating a mission

**From a GitHub issue** -- An issue labelled `soy-run` triggers the webhook.
HMAC-verified (default-deny). Ingestion is idempotent on the
`(source, external_id)` key so re-deliveries are safe. When Git-as-SSOT
is enabled, the webhook also creates the feature branch and writes a
placeholder `spec.md`.

**Without a GitHub issue** -- POST directly to the API. This is the
manual path and the most common way to work without a repo integration:

```bash
# 1. Create the mission (title + repo_url + branch_prefix are required)
curl -X POST http://localhost:8923/api/v1/missions \
  -H 'Content-Type: application/json' \
  -d '{
    "title": "Add rate limiting to the API",
    "description": "Implement token-bucket rate limiting on all public endpoints.",
    "repo_url": "https://github.com/org/repo",
    "branch_prefix": "feature/rate-limiting"
  }'

# 2. Transition to planning (triggers the PraisonAI planning agent)
curl -X POST http://localhost:8923/api/v1/missions/{id}/transition \
  -H 'Content-Type: application/json' \
  -d '{"to_status": "planning"}'

# 3. Approve planning (gates execution)
curl -X POST http://localhost:8923/api/v1/missions/{id}/approve \
  -H 'Content-Type: application/json' \
  -d '{"gate_type": "planning"}'

# 4. Transition to execution (agent team starts working)
curl -X POST http://localhost:8923/api/v1/missions/{id}/transition \
  -H 'Content-Type: application/json' \
  -d '{"to_status": "execution"}'
```

You can also skip straight to execution for ad-hoc work:

```bash
curl -X POST http://localhost:8923/api/v1/missions/{id}/transition \
  -H 'Content-Type: application/json' \
  -d '{"to_status": "execution"}'
```

This bypasses the planning gate -- useful when you already know what to
build and just want the agent team to run. No `repo_url` or `branch_prefix`
is required for manual missions; omit them if you don't need Git-as-SSOT.

### Agent team and task execution

Each mission gets a team of agents -- one per role (orchestrator, coder,
qa, reviewer). Each agent carries a model identifier, a sandbox flag, and
optional tool/system-prompt overrides.

Tasks are the units of work. Each task is assigned to one agent and may
declare dependencies on other tasks (forming a DAG). The worker resolves
this dependency graph into topological layers and executes them in order.
Independent tasks within a layer can run concurrently.

When a task fails, the worker retries up to 3 times with linear backoff.
After 3 failures the task is escalated, which also escalates the parent
mission.

The PraisonAI worker is the bridge between the DB and the agent runtime.
It constructs `praisonaiagents.Agent` instances with the right model,
tools (sandboxed agents get only file_read/file_write; unsandboxed also
get run_command and web_search), and submits tasks to PraisonAI's
workflow engine. A coding-agent dispatcher can also invoke external CLI
agents via subprocess, reading their configuration from JSON manifests.

### Model routing

Model identifiers are resolved at call time:
- `ollama/<name>` -- local Ollama instance, no API key needed
- `<name>:cloud` -- cloud endpoint, requires API key
- bare name -- treated as local Ollama

The resolver injects the correct `OPENAI_BASE_URL` and `OPENAI_API_KEY`
so PraisonAI's OpenAI-compatible client works without knowing which
backend it's talking to.

### Real-time events

The PraisonAI worker publishes lifecycle events (task completed, failed,
escalated, mission transitioned) to an in-memory event bus. WebSocket
clients subscribe per-mission (or globally with an admin token). Each
subscriber has a bounded queue; slow consumers have their oldest events
dropped rather than blocking the publisher.

### External integrations (all gated, all best-effort)

- **Mission Control** -- Soy pushes mission/agent/task state to MC's REST
  API. Tight timeout (2s); a slow or down MC never degrades Soy.
- **DeerFlow** -- Optional sandbox trigger for agents whose sandbox flag
  is set. Routes tool execution to DeerFlow instead of running locally.
- **Git-as-SSOT** -- Per-mission working clones, feature branches, spec.md
  commits, and PR management via `gh`. The merge transition auto-merges
  the PR when the feature is enabled.

---

## Running locally

```bash
# Install dependencies
pip install -r requirements.txt

# Run migrations and start the server
# (the lifespan hook runs alembic upgrade head automatically)
uvicorn soy.main:app --reload --host 127.0.0.1 --port 8923

# Or run migrations separately
alembic --config soy/alembic.ini upgrade head
```

The database URL is read from `SOY_DATABASE_URL` at runtime. When unset, Soy
falls back to `sqlite:///./soy_dev.db` so you can develop without a running
PostgreSQL instance.

To skip automatic migration on startup, set `SOY_RUN_MIGRATIONS_ON_STARTUP=false`.

---

## Tests

```bash
cd piperoni
python -m pytest repos/soy/tests -v
```

The test suite uses an in-memory SQLite database by default (no `SOY_TEST_DATABASE_URL`
needed). The `conftest.py` fixture resets the cached engine and worker singleton
between tests so each case gets a clean database. If `SOY_TEST_DATABASE_URL` is set
to a PostgreSQL URL, the suite runs against that instead.

Test coverage spans: mission CRUD and state machine, agent CRUD and team assembly,
task creation and execution with the 3-try retry rule, execution logs, the GitHub
webhook, WebSocket subscription and broadcast, coding-agent dispatch, Git-as-SSOT
branch/spec/PR operations, alembic migration chain idempotency, model resolver
routing (cloud vs local Ollama), MC sync and DeerFlow gating, and the JSON logging
formatter.
