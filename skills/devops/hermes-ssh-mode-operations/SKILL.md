---
name: hermes-ssh-mode-operations
description: "Use when operating Hermes SSH Mode: manage local/SSH backends with /ssh list/status/test/use/on/off, validate redacted targets, and verify model auto-switch policy."
version: 1.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [hermes, ssh, gateway, terminal, operations]
    related_skills: [hermes-agent, systematic-debugging]
---

# Hermes SSH Mode Operations

## Overview

Hermes SSH Mode lets a gateway section or thread run terminal, file, and code execution work through either the local backend or an SSH backend. Treat `local` and configured SSH targets as peer **execution backends**:

- `/ssh use <backend>` changes the current backend for the section. `<backend>` can be `local` or any configured SSH target alias.
- `/ssh on <backend>` allows the model to auto-switch to that backend.
- `/ssh off <backend>` marks that backend as not authorized for model entry. A model-initiated `ssh_mode use <backend>` must request user approval. It does **not** stop the current backend or clear an existing binding.

The SSH target list is explicit: Hermes reads its own target registry rather than silently importing every host from the user's OpenSSH config.

## When to Use

Use this skill when:

- A user asks what SSH targets/backends Hermes can use.
- A user wants selected OpenSSH hosts copied into Hermes' SSH target list.
- `/ssh list`, `/ssh status`, `/ssh test`, `/ssh use`, `/ssh on`, or `/ssh off` needs validation.
- A gateway section should be moved between `local` and an SSH backend.
- Model auto-switch permission must be checked or changed for a backend.

Do not use this skill to bulk-import private infrastructure indiscriminately or to read private key contents.

## Source Model

Hermes SSH Mode uses two separate sources:

| Source | Purpose | Loaded implicitly by `/ssh list`? |
|---|---|---|
| `~/.ssh/config` | System OpenSSH metadata source | No |
| `~/.hermes/ssh/targets.yaml` | Hermes-owned target registry | Yes |

This separation is deliberate. Import hosts explicitly so the user controls what becomes available to Hermes.

## Safe Metadata Import

When copying a host from OpenSSH config into the Hermes target registry, copy only runtime metadata:

- alias
- `HostName` as `host`
- `User` as `user`
- non-default `Port` as `port`
- `IdentityFile` path as `identity_file` but never key contents
- `IdentitiesOnly yes` as `identities_only: true`
- `UserKnownHostsFile` as `known_hosts`
- `StrictHostKeyChecking` as `host_key_policy`

Always redact identity paths in user-visible output:

```text
identity: [REDACTED_PATH]
```

## Verification Ladder

After adding or editing targets, verify in this order:

1. Load the registry through Hermes code:

   ```python
   from gateway.ssh_targets import load_ssh_targets
   targets = load_ssh_targets()
   ```

2. Render the list and confirm identity paths are redacted:

   ```python
   from gateway.ssh_targets import render_ssh_targets
   print(render_ssh_targets(targets))
   ```

3. Validate target completeness:

   ```python
   from gateway.ssh_targets import validate_ssh_target_for_runtime
   for target in targets:
       assert validate_ssh_target_for_runtime(target) is None
   ```

4. Ask OpenSSH to resolve aliases without connecting:

   ```bash
   ssh -G <alias>
   ```

5. If allowed, run a read-only smoke check with a short timeout:

   ```bash
   ssh -o BatchMode=yes -o ConnectTimeout=5 <alias> 'printf ok'
   ```

A timeout for one target usually means host/network state. It does not by itself prove that Hermes SSH Mode is broken.

## `/ssh` Backend Flow

In a gateway section or thread, use the flat first-level interface:

```text
/ssh list
/ssh status
/ssh test <backend>
/ssh use <backend> --cwd <remote-path>
/ssh on <backend>
/ssh off <backend>
```

Where `<backend>` is `local` or a configured SSH target alias.

Expected behavior:

- `/ssh list` shows `local` plus the Hermes-owned SSH target registry, with identity paths redacted.
- `/ssh status` starts as current backend `local` unless a section binding exists, and shows auto-switch policy for each backend.
- `/ssh test <backend>` validates a backend and does not change binding or auto-switch policy.
- `/ssh use <backend> [--cwd <remote-path>]` binds the current section to that backend. If `<backend>` is an SSH target and the command is sent from a Feishu parent chat, Hermes creates a new thread by default and binds that thread; `-t` / `--thread` is accepted as an explicit form but is not required.
- `/ssh on <backend>` authorizes model-initiated entry to that backend.
- `/ssh off <backend>` removes model-entry authorization for that backend. A model-initiated `ssh_mode use <backend>` must request approval; the current backend remains usable and current bindings are not cleared.

Do not reintroduce `/ssh local` or `/ssh yolo ...` as user-visible SSH Mode controls. Dangerous-command `/yolo` is a separate global approval bypass and must not be confused with SSH backend switching policy.

## Command Side Effects

Use this matrix when validating that SSH Mode changed the intended state and nothing else.

| Command | Reads | Writes files | Runtime side effects | Must not do |
|---|---|---|---|---|
| `/ssh list` | `~/.hermes/ssh/targets.yaml`; section policy | None | None; does not create terminal environments or change bindings | Must not read private key contents or implicit-import `~/.ssh/config` |
| `/ssh status` | `~/.hermes/ssh/bindings.json`; target registry | None | None; reports current backend and auto-switch policy | Must not change binding or create an SSH environment |
| `/ssh test local` | None beyond section context | None | None | Must not change binding or policy |
| `/ssh test <ssh-alias>` | Target registry | None | Validates target metadata completeness; no connection required | Must not change binding or policy |
| `/ssh use <backend>` where backend is `local` | Current section key | `~/.hermes/ssh/bindings.json` via `clear_ssh_binding()` | Clears section-scoped terminal override and evicts cached agent; execution uses the local/session default backend | Must not change auto-switch policy or delete targets |
| `/ssh use <backend>` where backend is an SSH target, in an existing section/thread | Target registry; current section key | `~/.hermes/ssh/bindings.json` via `set_ssh_binding()` | Registers section-scoped terminal overrides under the durable session key, evicts cached agent, and makes terminal/file/code tools use the selected SSH backend | Must not modify `~/.ssh/config`; must not read private key contents; must not affect other sections |
| `/ssh use <ssh-alias>` from a Feishu parent chat | Target registry; parent message metadata | `~/.hermes/ssh/bindings.json` for the newly created thread's section key | Creates a Feishu thread by default, binds that thread to SSH, and leaves the parent chat unbound | Must not require `-t`; must not bind the parent chat silently |
| `/ssh on <backend>` | Target registry if backend is SSH | `~/.hermes/ssh/bindings.json` backend policy | Authorizes model-initiated entry to that backend | Must not switch current backend |
| `/ssh off <backend>` | Target registry if backend is SSH | `~/.hermes/ssh/bindings.json` backend policy | Removes model-entry authorization for that backend; `ssh_mode use <backend>` must request approval; current backend remains usable | Must not clear current binding or stop tools from using the current backend |

Runtime note: system-level SSH Mode is `terminal.backend=ssh` plus `TERMINAL_SSH_*` config. Section-level SSH Mode must be equivalent from the model/tool perspective: `build_environment_hints()` should report `Terminal backend: ssh`, and `terminal`, `read_file`/`write_file`/`patch`/`search_files`, and `execute_code` should all resolve the same SSH environment for the section until the section is switched to another backend through `/ssh use <backend>`.

Control-plane boundary: SSH Mode intentionally affects the task execution surface, not Hermes' own profile state. Even when terminal/file/code execution is remote, `skill_view`/`skill_manage`, memory writes, cron definitions, session DB, gateway state, profile config, and skill loading are resolved through the local Hermes process and profile (`get_hermes_home()`, e.g. `~/.hermes/skills` and `~/.hermes/memories`). If the model needs to update its own skill after an SSH task, use `skill_manage`; do not try to edit `~/.hermes/skills` through the remote `terminal`/file tools unless the goal is explicitly to modify a remote Hermes installation.

## Model-Facing SSH Control

Slash commands are user-facing. The model-facing surface is the `ssh_mode` tool, which must be in the default/core tool schema and in the `terminal` toolset. Use it when the user asks the model to inspect SSH/backend state, list backends, test a backend, or request a backend switch.

`ssh_mode` actions mirror the slash command interface:

- `status`: read current backend and backend auto-switch policy.
- `list`: list `local` plus configured SSH targets with paths redacted.
- `test`: validate `local` or an SSH target without changing state.
- `use`: request switching to `local` or an SSH target. If that backend's auto-switch policy is off, Hermes must request user approval or return `approval_required`; it must not switch silently.
- `on` / `off`: user-owned policy changes. The model-facing tool must not grant or revoke its own switching permission; it should return `approval_required` and instruct the user to run `/ssh on <backend>` or `/ssh off <backend>`.

Important Feishu parent-chat boundary: the model tool cannot create Feishu Threads by itself. In a parent chat, `ssh_mode use <ssh-alias>` should report approval/instruction rather than silently binding the parent. Tell the user to run `/ssh use <backend>`; Hermes creates a Thread by default and binds SSH there.

This mirrors the broader Hermes affordance pattern: slash commands are explicit user controls, while model-visible tools are how the model discovers safe actions it may initiate. SSH needs both, or the model will not know how to enter/exit even if `/ssh` works.

## SSH Mode Test Strategy

A complete SSH Mode change needs four kinds of validation:

1. **Interaction mock tests**: simulate Feishu parent chat and thread events. Assert `/ssh use <alias>` without `-t` creates a thread by default, writes exactly the new thread binding, and does not require the user to remember a flag. Also simulate an existing Feishu thread and assert `/ssh use <other-alias>` rewrites that thread's binding without touching the parent chat or other threads.
2. **Backend-equivalence mock tests**: register section-scoped SSH overrides and assert prompt building plus terminal/file/code tools behave as if system-level `terminal.backend=ssh` were active. This must include: model environment hints reporting `Terminal backend: ssh`; terminal commands creating an SSH env; file tools using that same env for read/write/search/patch; `execute_code` using that same env; `/ssh use <backend>` returning work to the selected backend, including `local`; and control-plane tools such as `skill_view`/`skill_manage` plus memory remaining profile-local rather than being routed through SSH.
3. **Model-affordance tests**: assert `ssh_mode` appears in the default/core tool schema and in the `terminal` toolset; assert its schema exposes only `status`, `list`, `test`, `use`, `on`, and `off`; assert off-backend `use` returns `approval_required` or uses the platform approval card.
4. **Live read-only smoke**: when a permitted target exists, run a short `pwd && whoami && hostname` or `printf ok` through the Hermes terminal SSH backend. Use raw `ssh` only as a control, not as proof that Hermes tool routing works.

Minimum acceptance scenarios:

- **From scratch in a Feishu parent chat**: `/ssh use <alias>` creates a thread, writes only the new thread's binding, returns a message that says tools in that thread use SSH, and subsequent work in the parent chat remains local.
- **Inside an existing Feishu thread**: `/ssh use <alias>` updates the current thread binding; `/ssh status` reports the selected backend; the next prompt build and terminal/file/code tools all see SSH.
- **Switching targets inside a thread**: a second `/ssh use <other-alias>` replaces the binding, refreshes runtime overrides, evicts the cached agent for that section, and tools use the new target.
- **Switching to another backend**: `/ssh use <backend>` applies uniformly; when `<backend>` is `local`, the current SSH binding and runtime overrides are cleared as part of the normal `use` semantics.
- **Policy off**: `/ssh off <backend>` does not stop the current backend. It removes model-entry authorization for that backend, so model-side `ssh_mode use <backend>` must request approval.

## Gateway Restart Check

If `/ssh` is unrecognized after code or command registry changes, check gateway logs and restart state. A valid check looks for both:

- gateway stop/start/connect lines after restart; and
- a later `/ssh list` without `Unrecognized slash command /ssh`.

Do not claim a restart succeeded without direct command output or log evidence.

## Promotion Path

For durable workflow capture, promote in layers:

1. Local experiment or local skill, with machine-specific scripts and notes.
2. Shared/internal skill, with host details generalized.
3. Repo-bundled skill, on a fresh branch, with no machine-specific target registry or private identity paths.

The repo-bundled skill should teach the operating procedure. It should not ship a private host list.

## Common Pitfalls

1. **Assuming `/ssh list` reads `~/.ssh/config`.** It does not. Import selected hosts into the Hermes registry first.
2. **Leaking private key paths.** Store paths only if needed for runtime, but render them as `[REDACTED_PATH]` in summaries.
3. **Treating one host timeout as a Hermes failure.** Check other targets and use `ssh -G` to separate config resolution from network reachability.
4. **Assuming parent-chat `/ssh use` should fail without `-t`.** In Feishu parent chats, `/ssh use <alias>` should create a thread by default and bind SSH there; `-t` / `--thread` is only the explicit form.
5. **Confusing `/ssh off <backend>` with switching away.** `off` removes model-entry authorization for that backend; switching is always done with `/ssh use <backend>`.
6. **Confusing global `/yolo` with SSH Mode.** `/yolo` is dangerous-command approval bypass. SSH Mode backend policy uses `/ssh on <backend>` and `/ssh off <backend>`.

## Verification Checklist

- [ ] Workspace and current profile are confirmed.
- [ ] Target/backend selection is explicit.
- [ ] Private key contents were not read.
- [ ] Identity paths are redacted in user-visible output.
- [ ] `load_ssh_targets()` and `render_ssh_targets()` pass.
- [ ] `validate_ssh_target_for_runtime()` reports no missing host/user for SSH targets.
- [ ] `ssh -G <alias>` resolves expected host/user/port when checking an SSH target.
- [ ] Read-only smoke checks were run when appropriate.
- [ ] `/ssh list/status/test/use/on/off` flow was verified.
- [ ] Any repo-bundled change is on a branch and contains no private target list.
