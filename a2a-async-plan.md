# A2A Async Plan: arq-backed Task Execution

## Implementation Status

Last updated: 2026-04-30

Rapha loop Round 1 is complete and verified:

- Added arq/sse dependencies under the `a2a` extra.
- Added Redis task facade, arq worker, async JSON-RPC dispatch, task get/cancel/events routes, and gated async MCP tools.
- Standardized queue env on `A2A_QUEUE_NAME`, with `A2A_ARQ_QUEUE_NAME` kept as a compatibility alias.
- Standardized the queue model on `clawcu:a2a:<instance-name>`.
- Standardized deployment on shared Redis container `clawcu-a2a-redis` and default `A2A_REDIS_URL=redis://host.docker.internal:6379/0`.
- Integrated lifecycle start order: main service -> Redis -> HTTP adapter -> worker.
- Verified with `uv run --extra a2a pytest tests/a2a_adapter tests/test_service.py` (`227 passed`).

Rapha loop Round 2 is complete and verified:

- Surface async/Redis/queue/worker status in `inspect`.
- Update env/protocol docs and release notes.
- Run CLI and Redis smoke verification.
- Verified with `uv run --extra a2a pytest` (`509 passed`).
- Redis smoke verified against `clawcu-a2a-redis`: task snapshots/events and worker completion round-trip through real Redis.

Rapha loop Round 3 is complete and verified:

- Change async API/MCP tools to default-on by setting `A2A_ASYNC_ENABLED`'s default to true.
- Preserve `A2A_DEFAULT_MODE=sync`, so plain JSON-RPC calls and `a2a_call_peer` still block unless the caller explicitly requests async.
- Preserve `A2A_ASYNC_ENABLED=false` as the explicit rollback switch that hides async MCP tools and rejects non-blocking submissions.
- Verified with `uv run --extra a2a pytest` (`511 passed`).

Rapha loop Round 4 is planned:

- Move the A2A registry from a host foreground/LaunchAgent process into a managed Docker service named `clawcu-a2a-registry`.
- Reuse the shared `clawcu-a2a-redis` container as the registry state store.
- Store peer card/status/endpoint/last-seen state in Redis so registry restarts do not lose discovery data and adapters can publish health independently of registry process lifetime.
- Keep HTTP discovery at `http://127.0.0.1:9100/agents` for compatibility with existing adapters and MCP tools.

Final rollout state:

- Async is enabled by default for A2A instances; set `A2A_ASYNC_ENABLED=false` to disable the async surface.
- Default JSON-RPC mode remains `sync`.
- CLI task subcommands are deferred; HTTP task endpoints and MCP async tools are the supported operator/user surfaces for this rollout.
- Local commits: `Add Redis-backed A2A async tasks`; `Enable A2A async by default`.

## Next Target: Dockerized Redis-backed Registry

Current registry status:

- `clawcu a2a registry serve` is a host process.
- It discovers peer cards by probing running instance adapter ports.
- It has no durable state and can disappear when the terminal/session dies unless externally supervised.

Target registry architecture:

```text
A2A adapter companions  ---- publish card/status ----> Redis registry state
                                                          ^
                                                          |
MCP tools / A2A peers ---- GET /agents ----------------> A2A registry container
                                                          |
                                                          v
                                                  clawcu-a2a-redis
```

Planned services:

```text
clawcu-a2a-redis       shared Redis, already used by async tasks
clawcu-a2a-registry    shared registry container, binds 127.0.0.1:9100
clawcu-a2a-<instance>  per-instance HTTP adapter
clawcu-a2a-worker-<instance> per-instance arq worker
```

Redis registry state model:

```text
a2a:registry:peer:<name>       JSON card/status snapshot
a2a:registry:peers             Redis Set of peer names
a2a:registry:event:<name>      optional stream for card/status changes
```

Peer snapshot fields:

```json
{
  "name": "steve.jobs",
  "endpoint": "http://host.docker.internal:18800",
  "role": "A2A agent",
  "skills": ["chat", "tools"],
  "status": "running",
  "source": "adapter",
  "updated_at": "...",
  "expires_at": "..."
}
```

Compatibility requirements:

- `/agents` and `/agents/{name}` response shapes remain unchanged.
- `A2A_REGISTRY_URL=http://host.docker.internal:9100` remains valid inside adapter/worker containers.
- Existing MCP tools continue to call the registry over HTTP.
- If Redis is temporarily unavailable, registry should return a clear 503 rather than an empty peer list.

Implementation scope:

1. Add registry Redis store module and tests.
2. Extend the registry HTTP server to read peer snapshots from Redis, with optional probe fallback during migration.
3. Add a registry Docker entrypoint/image path or reuse the adapter image with a registry command.
4. Add service lifecycle helpers to ensure Redis and `clawcu-a2a-registry` are running before A2A adapters.
5. Add adapter-side publish/heartbeat and cleanup/TTL behavior.
6. Update CLI/operator surface to manage the Docker registry instead of relying on a host process.
7. Update docs, release notes, and troubleshooting.

## Goal

Replace the old baked-sidecar async task implementation with an arq-backed design for the current companion adapter model.

The target is to support long-running A2A calls without blocking MCP clients or JSON-RPC callers, while preserving the A2A task semantics users already need:

- async dispatch returns quickly with a `task_id`
- task state can be queried
- completed tasks expose the peer reply
- failed tasks expose useful errors
- cancellation is supported where possible
- progress can be streamed or polled
- task state survives adapter process restarts

## Current State

Current companion adapter behavior is synchronous:

- `POST /` accepts JSON-RPC `message/send`.
- The adapter calls the local service gateway via `/v1/chat/completions`.
- The HTTP request waits until the gateway returns.
- The MCP bridge exposes `a2a_call_peer` and `a2a_list_peers`, but no async task tools.

The old sidecar implementation had async support, but it was local to the sidecar process:

- `POST /a2a/send` accepted `mode=async`.
- A local `TaskStore` wrote snapshots and JSONL events to disk.
- A local `ThreadPoolExecutor` ran tasks in the background.
- `GET /a2a/tasks/<task_id>` returned snapshots.
- `POST /a2a/tasks/<task_id>/cancel` marked tasks canceled.
- `GET /a2a/tasks/<task_id>/events` streamed Server-Sent Events.

The arq design should keep the protocol shape but move queueing and worker execution out of the HTTP adapter process.

## Target Architecture

```text
A2A caller
   |
   | JSON-RPC message/send
   v
A2A HTTP adapter  ---- enqueue job ----> Redis / arq queue
   |                                      ^
   | task get / task cancel / SSE         |
   v                                      |
Redis task state + events <----- A2A arq worker
                                      |
                                      | /v1/chat/completions
                                      v
                              local service gateway
```

### Components

1. Redis
   - Shared per host, not per instance.
   - Stores arq queues, arq job metadata, task snapshots, and task events.
   - Proposed container name: `clawcu-a2a-redis`.

2. A2A HTTP adapter
   - One companion container per A2A-enabled instance, as today.
   - Exposes AgentCard, JSON-RPC A2A endpoint, and MCP endpoint.
   - Owns protocol translation and task API surface.
   - Enqueues async work into the instance-specific arq queue.

3. A2A arq worker
   - One worker container per A2A-enabled instance.
   - Shares the main service container network namespace, same as the adapter.
   - Listens only to that instance's queue.
   - Calls the local gateway at `http://127.0.0.1:<gateway_port>/v1/chat/completions`.

## Queue Model

Use one arq queue per instance:

```text
clawcu:a2a:<instance-name>
```

Reasoning:

- The worker needs instance-local gateway URL, auth token, role, and persona context.
- A global queue would allow one instance worker to pick up another instance's job.
- Per-instance queues keep isolation simple and make debugging easier.

Use the A2A `task_id` as the arq `_job_id`.

```text
task_id = "task_" + uuid4 hex
arq job id = task_id
```

This gives a stable ID across:

- A2A task APIs
- MCP async tools
- arq job lookup
- logs
- Redis keys

## Redis Data Model

arq stores its own queue and job metadata. We should not expose arq internals as the public task model.

Keep a small A2A-owned task facade:

```text
a2a:task:<task_id>              JSON snapshot
a2a:task:<task_id>:events       Redis Stream
a2a:task-index:<instance-name>  Redis Set of task_ids, optional
```

Snapshot shape:

```json
{
  "task_id": "task_...",
  "instance": "analyst",
  "peer": "writer",
  "state": "submitted",
  "created_at": "...",
  "updated_at": "...",
  "thread_id": "optional",
  "request_id": "optional",
  "input": {
    "message": "..."
  },
  "result": null,
  "error": null,
  "last_progress_at": null,
  "last_progress_message": null
}
```

States:

```text
submitted -> working -> completed
submitted -> working -> failed
submitted -> working -> canceled
submitted -> canceled
```

Events are appended to the Redis Stream:

```json
{"event": "submitted", "state": "submitted", "ts": "..."}
{"event": "working", "state": "working", "ts": "..."}
{"event": "progress", "message": "calling gateway", "ts": "..."}
{"event": "completed", "state": "completed", "result": {...}, "ts": "..."}
```

Use TTLs for snapshot and stream keys based on `A2A_TASK_RETAIN_S`.

## HTTP / A2A Surface

### JSON-RPC `message/send`

Support both sync and async modes.

Mode selection:

- explicit JSON-RPC metadata or extension field if available
- fallback env: `A2A_DEFAULT_MODE=sync|async`
- MCP sync tool must force sync
- MCP async tool must force async

Async response:

```json
{
  "id": "task_<id>",
  "status": {"state": "submitted"},
  "metadata": {
    "task_id": "task_<id>",
    "request_id": "..."
  }
}
```

Sync response remains the current completed task response.

### Task Query

Add a task read endpoint compatible with the current adapter routing style.

Candidate routes:

- `GET /tasks/{task_id}`
- optionally `POST /` JSON-RPC task method if the A2A SDK expects one

Return the A2A-owned snapshot, with arq status used only as a fallback.

Mapping:

```text
snapshot completed -> completed
snapshot failed    -> failed
snapshot canceled  -> canceled
arq queued         -> submitted
arq in_progress    -> working
missing            -> 404
```

### Task Cancel

Add:

```text
POST /tasks/{task_id}/cancel
```

Behavior:

1. Mark A2A snapshot as `canceled` if not terminal.
2. Call `Job(task_id).abort()`.
3. Return the updated snapshot.

Worker settings must set:

```python
allow_abort_jobs = True
```

Cancellation is best effort:

- queued jobs can be prevented from running
- running jobs receive asyncio cancellation
- the local gateway may or may not stop its own model call when the client disconnects

### Task Events

Add:

```text
GET /tasks/{task_id}/events
```

Use `sse-starlette` and Redis Streams.

Support:

- replay from `Last-Event-ID`
- heartbeat frames
- terminal `end` frame
- idle timeout

## MCP Surface

Keep existing tools:

- `a2a_call_peer`
- `a2a_list_peers`

Add async tools:

- `a2a_call_peer_async`
- `a2a_get_task`
- `a2a_cancel_task`

MCP flow:

```text
a2a_call_peer_async
  -> registry lookup
  -> peer JSON-RPC message/send with async mode
  -> return task_id immediately

a2a_get_task
  -> registry lookup
  -> GET peer /tasks/{task_id}
  -> return state
  -> include reply text in content when completed

a2a_cancel_task
  -> registry lookup
  -> POST peer /tasks/{task_id}/cancel
  -> return updated state
```

The sync tool should force sync mode so `A2A_DEFAULT_MODE=async` cannot silently change existing behavior.

## Worker Design

Worker function:

```python
async def run_gateway_turn(ctx, payload):
    task_id = ctx["job_id"]
    await task_store.transition(task_id, "working")
    await task_store.progress(task_id, "waiting for gateway")
    reply = await call_gateway(payload)
    await task_store.transition(task_id, "completed", result={"reply": reply})
    return {"reply": reply}
```

Worker settings:

```python
class WorkerSettings:
    functions = [run_gateway_turn]
    redis_settings = REDIS_SETTINGS
    queue_name = f"clawcu:a2a:{INSTANCE_NAME}"
    max_jobs = A2A_TASK_WORKERS
    job_timeout = A2A_TASK_DEADLINE_S
    keep_result = A2A_TASK_RETAIN_S
    max_tries = 1
    retry_jobs = False
    allow_abort_jobs = True
```

Important arq behavior:

- arq uses pessimistic execution.
- If a worker shuts down during a job, the job may run again later.
- LLM calls are not naturally idempotent.

Mitigation:

- Use `max_tries=1` and `retry_jobs=False` by default.
- Before calling gateway, check snapshot state.
- If snapshot is already terminal, return without calling gateway.
- After gateway returns, write terminal state only if the snapshot is still non-terminal.

## Deployment Model

Preferred production shape:

```text
clawcu-a2a-redis
clawcu-a2a-<instance>          HTTP adapter
clawcu-a2a-worker-<instance>   arq worker
clawcu-<service>-<instance>    main service
```

The adapter and worker both use:

```text
--network container:<main-service-container>
```

Redis access:

```text
A2A_REDIS_URL=redis://host.docker.internal:<port>/<db>
```

The service should start:

1. main service container
2. Redis if missing
3. HTTP adapter companion
4. arq worker companion

Restart should restart adapter and worker after the main service.

Stop/remove should stop both companions.

## Configuration

New envs:

```text
A2A_ASYNC_ENABLED=true|false
A2A_DEFAULT_MODE=sync|async
A2A_REDIS_URL=redis://host.docker.internal:6379/0
A2A_TASK_WORKERS=4
A2A_TASK_DEADLINE_S=86400
A2A_TASK_RETAIN_S=86400
A2A_TASK_PROGRESS_INTERVAL_S=3
A2A_TASK_EVENTS_IDLE_TIMEOUT_S=60
```

Existing envs to preserve:

```text
A2A_GATEWAY_URL
A2A_GATEWAY_AUTH_TOKEN
A2A_GATEWAY_READY_PATH
A2A_GATEWAY_TIMEOUT
A2A_SEND_TIMEOUT
A2A_REGISTRY_URL
A2A_REGISTRY_TOKEN
```

## Implementation Steps

1. Add dependencies
   - Add `arq` to the `a2a` optional dependency group.
   - Confirm Redis client compatibility through arq.

2. Add task facade module
   - `src/clawcu/a2a/adapter/tasks.py`
   - Redis snapshot operations
   - Redis Stream event operations
   - state transition validation
   - TTL handling

3. Add arq worker module
   - `src/clawcu/a2a/adapter/worker.py`
   - `WorkerSettings`
   - `run_gateway_turn`
   - shared gateway call logic with the HTTP adapter

4. Extend HTTP adapter
   - support async `message/send`
   - add task get route
   - add cancel route
   - add SSE events route

5. Extend MCP bridge
   - add `a2a_call_peer_async`
   - add `a2a_get_task`
   - add `a2a_cancel_task`
   - force sync mode for `a2a_call_peer`

6. Extend companion orchestration
   - add Redis ensure/start helper
   - add worker companion spec
   - start/stop/restart worker with adapter
   - include env wiring for Redis and queue name

7. Tests
   - unit tests for task facade state transitions
   - unit tests for MCP async tools
   - server tests for async message/send
   - cancel behavior tests
   - SSE replay tests using Redis Streams or a fake
   - orchestration tests for worker companion start/stop

8. Docs
   - update A2A protocol docs
   - document Redis requirement
   - document async mode, task polling, cancel semantics, and retention knobs

## Rollout Strategy

Phase 1: Hidden async plumbing

- Add arq worker and task facade.
- Keep default mode sync.
- Initially keep async MCP tools behind `A2A_ASYNC_ENABLED=true`.

Phase 2: Opt-in async

- Enable `a2a_call_peer_async` for A2A instances with Redis available.
- Keep sync tool behavior unchanged.
- Add inspect output showing async status and Redis URL.

Phase 3: Default-on async

- Make `A2A_ASYNC_ENABLED` default to true for new A2A instances.
- Keep `A2A_DEFAULT_MODE=sync` unless there is a strong compatibility reason to flip it.

## Risks

1. Redis becomes required for async.
   - Mitigation: sync path still works; operators can set `A2A_ASYNC_ENABLED=false` to hide the async surface if Redis is unavailable.

2. arq jobs can rerun after worker cancellation.
   - Mitigation: single try by default, snapshot idempotency guard, no gateway call if task is terminal.

3. Running cancel may not stop the downstream model.
   - Mitigation: document best-effort semantics and ensure local HTTP request is canceled promptly.

4. More containers per instance.
   - Mitigation: keep Redis shared per host; make worker optional when async disabled.

5. Result shape drift between arq and A2A.
   - Mitigation: expose only A2A-owned snapshots, not raw arq internals.

## Open Questions

- Should Redis be managed automatically by ClawCU, or should operators provide `A2A_REDIS_URL`?
- Should async be enabled automatically for all `--a2a` instances once Redis is available?
- Should task endpoints follow A2A SDK-native task routes, custom REST routes, or both?
- Should progress events include partial model output, or only coarse milestones?
- Should worker be a separate container or a second process inside the adapter container?
