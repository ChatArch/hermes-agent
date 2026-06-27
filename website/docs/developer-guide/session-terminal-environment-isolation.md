---
title: Session-scoped terminal environments
sidebar_label: Session terminal isolation
---

# Session-scoped terminal environments

Hermes keeps conversation history, gateway routing, and many runtime decisions scoped to a **session**. Terminal-like tool environments must follow the same boundary: a virtualenv, `PATH`, exported variable, current directory, SSH backend, or container snapshot created in one chat/thread session must not silently leak into another session.

This document explains the mechanism behind the session boundary, the tool-call runtime, and the terminal environment cache. It also documents the isolation rule implemented for terminal/code/file environments.

## One-sentence summary

Terminal environment reuse is still persistent **within one session**, but the default environment cache key is now derived from `HERMES_SESSION_KEY` when a session exists, so different gateway sessions do not share the same shell snapshot.

## Concepts at a glance

| Concept | Code | Purpose | Isolation boundary |
|---|---|---|---|
| Conversation session | `gateway/session.py::build_session_key` | Builds deterministic session keys from platform/chat/thread/user metadata | Chat/thread/session policy |
| Runtime session context | `gateway/session_context.py` | Stores session fields in `ContextVar`s for concurrent gateway turns | Python task/context |
| Tool dispatch | `model_tools.py`, `tools/registry.py`, individual tool handlers | Invokes tools with a `task_id` and active session context | Tool-call turn + task/session metadata |
| Terminal environment cache | `tools/terminal_tool.py::_active_environments` | Reuses local/SSH/container environments between commands | Resolved environment key |
| Environment snapshot | `tools/environments/base.py` | Persists shell exports, cwd, functions, aliases, and options | One environment instance |
| Code execution environment | `tools/code_execution_tool.py::_get_or_create_env` | Reuses the terminal environment for remote/local Python execution | Same resolved environment key |
| File-tool environment | `tools/file_tools.py` | Uses terminal/task overrides and live environment cwd for path/backend resolution | Same task/session override model |
| SSH/session binding | `tools/ssh_mode_tool.py`, `gateway/ssh_bindings.py` | Lets a session request an SSH backend when authorized | Gateway session key + hard backend override |

## Why this exists

The original terminal environment design intentionally provided a long-lived workspace:

- one bash-like working area;
- one current working directory;
- one set of installed packages;
- one exported environment state;
- one container/SSH/local backend reused across several commands.

That design is valuable for a single CLI session or one active coding task. For example, a user can run `cd project`, `export FOO=1`, install a package, and expect the next terminal command to see the same state.

The problem appears in a multi-session gateway process. Feishu, Slack, Discord, Telegram, ACP, and cron-style entrypoints can run different conversations inside the same Hermes process. Their conversation history may be isolated, while the terminal cache can still collapse to the same `default` environment if no better key is used. In that case:

1. Session A activates a project virtualenv or switches to SSH.
2. The terminal backend writes `PATH`, `VIRTUAL_ENV`, cwd, and shell exports back to the shared environment snapshot.
3. Session B runs a later terminal command.
4. Session B inherits Session A's shell state even though its conversation history is separate.

The fix is not to remove persistence. The fix is to move persistence to the same boundary as the conversation: **the Hermes session**.

## Tool-call mechanism

### Registration

Tools are registered through the central tool registry, usually in a module under `tools/`:

```python
registry.register(
    name="terminal",
    toolset="terminal",
    schema=TERMINAL_SCHEMA,
    handler=lambda args, **kw: terminal_tool(..., task_id=kw.get("task_id")),
)
```

The model sees schemas, requests tool calls, and Hermes routes those calls into registered handlers. The handler receives arguments and runtime metadata such as `task_id`.

### `task_id` is not always a session

`task_id` is useful, but it is not equivalent to a user-facing conversation session:

- top-level tools may have `task_id=None`;
- delegate/subagent tools may have a subagent-specific task id;
- ACP or gateway surfaces may use session ids as task ids for some operations;
- benchmark/runtime integrations may pass task ids that intentionally request isolated containers.

Therefore, terminal environment keying cannot blindly use raw `task_id` for every call. Some task ids are UI/task bookkeeping, while others are hard runtime isolation requests.

## Conversation isolation mechanism

Gateway session identity is constructed in `gateway/session.py::build_session_key`.

Examples of inputs:

- platform (`feishu`, `discord`, `telegram`, `local`, ...);
- chat type (`dm`, `group`, `channel`, `thread`);
- chat id;
- native thread/topic id;
- user id or platform-specific alternate id;
- configuration for per-user group/thread isolation.

The resulting session key is used to decide which agent/session history receives the message.

Runtime access to session fields is handled in `gateway/session_context.py`. It uses `ContextVar`s rather than process-global `os.environ` because the gateway can handle multiple concurrent messages in one Python process.

Important fields include:

```python
HERMES_SESSION_PLATFORM
HERMES_SESSION_CHAT_ID
HERMES_SESSION_THREAD_ID
HERMES_SESSION_KEY
HERMES_SESSION_ID
HERMES_SESSION_MESSAGE_ID
```

The helper `get_session_env(name, default="")` is the compatibility boundary:

1. If a ContextVar value exists, return it.
2. Otherwise fall back to `os.environ` for CLI, cron, tests, and older entrypoints.
3. Otherwise return the default.

Terminal isolation must use this helper rather than reading only `os.environ`, because direct environment variables are process-global and can be stale under concurrency.

## Tool environment mechanism

### Environment cache

`tools/terminal_tool.py` keeps a process-level cache:

```python
_active_environments: Dict[str, Any] = {}
```

The key in this dictionary is the resolved environment id. The value is an environment object such as a local shell, Docker container, Modal sandbox, Daytona environment, Singularity environment, or SSH environment.

### Snapshot persistence

The environment implementation persists shell state. The local backend creates a shell snapshot and then reuses it:

- source prior snapshot before command execution;
- execute the command;
- record exported variables, shell options, aliases, functions, and cwd back into the snapshot.

This is why terminal state persists between commands. It is also why sharing the wrong environment key across sessions causes leaks.

### Code execution shares the same boundary

`tools/code_execution_tool.py::_get_or_create_env` imports and reuses terminal environment helpers:

- `_active_environments`
- `_resolve_container_task_id`
- `resolve_task_overrides`
- `apply_task_env_overrides`
- `_create_environment`

That means a change to terminal environment keying also governs the environment used by `execute_code` for local/remote execution. This is intentional: terminal and code execution should see the same session-local backend and workspace state.

### File tools use the same override model

File tools need to resolve relative paths, live cwd, and remote backend operations. They read task overrides and terminal/file operation caches so that a terminal `cd` or SSH binding can be reflected in file operations. The exact file operation cache is separate, but the backend/cwd override model must stay aligned with terminal env keying.

## Isolation rule

The environment key resolution rule is:

1. If a task has a **hard runtime isolation override**, use the raw `task_id`.
2. Otherwise, if there is an active `HERMES_SESSION_KEY`, use `session:<HERMES_SESSION_KEY>`.
3. Otherwise, use `default` for legacy CLI/no-session behavior.

In code, this is centered in `tools/terminal_tool.py::_resolve_container_task_id`.

### Hard runtime overrides

The following override keys mean the caller explicitly requested an isolated backend/runtime:

```python
docker_image
modal_image
singularity_image
daytona_image
env_type
```

These continue to win over session keying. This preserves behavior for:

- SSH mode;
- Docker/Modal/Daytona/Singularity sandboxes;
- benchmark rollouts;
- any task that intentionally needs a distinct backend image.

### CWD-only overrides

CWD-only overrides are not hard isolation. They are used to pin workspace location for a session/task. With session-scoped terminal environments, a CWD-only override follows the active session key instead of forcing a raw task-id environment.

### No-session fallback

When there is no active session key, Hermes keeps the previous behavior and uses `default`. This preserves the single CLI/single task user experience.

## Before and after

### Before

```text
Session A terminal -> _resolve_container_task_id(None) -> default
Session B terminal -> _resolve_container_task_id(None) -> default
```

Both sessions share `_active_environments["default"]`.

### After

```text
Session A terminal -> _resolve_container_task_id(None) -> session:<A>
Session B terminal -> _resolve_container_task_id(None) -> session:<B>
CLI terminal       -> _resolve_container_task_id(None) -> default
SSH override task  -> _resolve_container_task_id(task) -> task
```

Session A and Session B keep independent shell snapshots, while CLI and hard override behavior remains compatible.

## Code walkthrough

### Reading the session key

The terminal layer reads session state through `gateway.session_context.get_session_env`:

```python
def _current_session_environment_key() -> str:
    try:
        from gateway.session_context import get_session_env
        session_key = get_session_env("HERMES_SESSION_KEY", "")
    except Exception:
        session_key = os.getenv("HERMES_SESSION_KEY", "")

    session_key = str(session_key or "").strip()
    if not session_key:
        return ""
    return f"session:{session_key}"
```

The fallback keeps legacy process-env entrypoints working, but ContextVar session state wins when present.

### Resolving the environment key

```python
def _resolve_container_task_id(task_id: Optional[str]) -> str:
    if task_id and task_id in _task_env_overrides:
        overrides = _task_env_overrides[task_id]
        if set(overrides.keys()) & _ISOLATION_KEYS:
            return task_id

    session_env_key = _current_session_environment_key()
    if session_env_key:
        return session_env_key

    return "default"
```

This is deliberately small. The main compatibility decision is the order:

1. hard backend isolation first;
2. session key second;
3. default last.

### Resolving task overrides

`resolve_task_overrides` still reads the raw task id first. That is important because `register_task_env_overrides` stores overrides under the raw id that the caller registered. If the environment key later resolves to `session:<key>`, the raw override must not disappear.

The lookup order is:

```python
raw task id -> resolved environment id -> {}
```

This keeps terminal, file, and code execution layers aligned.

## SSH mode compatibility

Recent SSH mode work adds `tools/ssh_mode_tool.py` and session-level SSH binding APIs. The SSH mode request registers task environment overrides with `env_type: "ssh"` and SSH parameters.

Because `env_type` is a hard isolation key, SSH mode continues to resolve to the raw task/session id used by the binding. Session-scoped default local environments therefore do not weaken SSH isolation.

Tests covering this include:

- `tests/tools/test_ssh_runtime_overrides.py`
- `tests/tools/test_ssh_mode_tool.py`
- `tests/gateway/test_ssh_command.py`

## Tests added

The regression suite for this behavior lives in:

```text
tests/tools/test_terminal_session_isolation.py
```

It covers:

1. ContextVar session key maps to `session:<key>`.
2. Legacy process `HERMES_SESSION_KEY` fallback works in a fresh context.
3. Different gateway sessions produce different environment keys.
4. The same gateway session reuses the same environment key.
5. No-session CLI calls still use `default`.
6. Backend/image hard override still returns raw task id.
7. CWD-only override follows the active session key.
8. `terminal_tool()` actually creates separate environment objects across sessions.
9. `terminal_tool()` reuses an environment inside one session.
10. `code_execution` uses the same session-scoped environment keying.

The tests intentionally use fresh `contextvars.Context()` helpers so they do not pollute existing approval or sudo fallback tests. This matters because `clear_session_vars()` marks ContextVars as explicitly empty to suppress stale `os.environ` fallback.

## Verification commands

Focused verification:

```bash
python -m pytest tests/tools/test_terminal_session_isolation.py -q
python -m pytest \
  tests/tools/test_terminal_session_isolation.py \
  tests/tools/test_terminal_tool.py \
  tests/tools/test_terminal_task_cwd.py \
  tests/tools/test_ssh_runtime_overrides.py \
  tests/tools/test_ssh_mode_tool.py \
  -q
python -m pytest \
  tests/gateway/test_ssh_command.py \
  tests/gateway/test_command_bypass_active_session.py \
  tests/gateway/test_interrupt_command.py \
  tests/gateway/test_feishu.py \
  -q
python -m compileall -q tools/terminal_tool.py tests/tools/test_terminal_session_isolation.py
git diff --check
```

## Operational notes

### What this fixes

This prevents cross-session leaks such as:

- a virtualenv activated in one Feishu thread affecting another thread;
- `PATH` changes from one session changing the Python found by another session;
- session-local cwd or exported variables unexpectedly appearing in unrelated sessions;
- local terminal and code execution disagreeing about the active session environment.

### What this does not change

This does not make terminal commands stateless. It does not reset every command. It does not disable shell snapshot persistence. It only moves persistence from the process-global `default` lane to the session lane when a session key exists.

### When to use hard overrides

Use task environment overrides with hard isolation keys when the runtime itself must be separate, such as SSH or containerized benchmark execution. Session keying is for default environment reuse; hard overrides are for backend/runtime replacement.

## Review checklist

When changing this area in the future, verify all of the following:

- Conversation session key construction still lives in `gateway/session.py`.
- Runtime session reads use `gateway.session_context.get_session_env`, not direct `os.environ`, in concurrent gateway paths.
- Hard backend overrides continue to win over session keying.
- No-session CLI behavior still uses `default`.
- `terminal`, `execute_code`, and file tools agree on task/session overrides.
- Tests use fresh ContextVar contexts and do not leave session variables set globally.
- SSH mode tests pass after any terminal keying change.

## Related files

- `gateway/session.py`
- `gateway/session_context.py`
- `tools/terminal_tool.py`
- `tools/environments/base.py`
- `tools/code_execution_tool.py`
- `tools/file_tools.py`
- `tools/ssh_mode_tool.py`
- `gateway/ssh_bindings.py`
- `tests/tools/test_terminal_session_isolation.py`
- `tests/tools/test_ssh_runtime_overrides.py`
- `tests/tools/test_ssh_mode_tool.py`
- `tests/gateway/test_ssh_command.py`
