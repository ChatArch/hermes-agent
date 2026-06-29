---
name: hermes-ssh-mode-operations
description: "Use when operating Hermes SSH Mode: explicitly import SSH target metadata, validate redacted targets, bind a section to SSH, and return it to local mode."
version: 1.0.0
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

Hermes SSH Mode lets a gateway section or thread run future terminal, file, and code execution work through an SSH backend. The SSH target list is explicit: Hermes reads its own target registry rather than silently importing every host from the user's OpenSSH config.

Use this skill to safely discover SSH metadata, add selected targets to the Hermes registry, validate them without leaking private paths, and operate the `/ssh` binding flow.

## When to Use

Use this skill when:

- A user asks what SSH targets Hermes can use.
- A user wants selected OpenSSH hosts copied into Hermes' SSH target list.
- `/ssh list`, `/ssh status`, `/ssh test`, `/ssh use`, or `/ssh local` needs validation.
- A gateway section should be moved to SSH backend temporarily and then returned to local mode.
- A one-off SSH Mode procedure needs to be turned into durable operating knowledge.

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

## `/ssh` Binding Flow

In a gateway section or thread, use:

```text
/ssh list
/ssh status
/ssh test <alias>
/ssh use <alias> --cwd <remote-path>
/ssh status
/ssh local
/ssh status
```

Expected behavior:

- `/ssh list` shows the Hermes-owned target registry, with identity paths redacted.
- `/ssh status` starts as local/no binding unless already configured.
- `/ssh test <alias>` validates configuration and does not change binding.
- `/ssh use <alias> --cwd <remote-path>` binds the current section to SSH backend. In a Feishu parent chat, it creates a new thread by default and binds that thread; `-t` / `--thread` is accepted as an explicit form but is not required.
- The next `/ssh status` shows backend `ssh`, selected alias, host/user/port, cwd, and redacted identity.
- `/ssh local` clears the binding and returns to local backend. `/ssh off` is a compatibility alias only.

## Command Side Effects

Use this matrix when validating that SSH Mode changed the intended state and nothing else.

| Command | Reads | Writes files | Runtime side effects | Must not do |
|---|---|---|---|---|
| `/ssh list` | `~/.hermes/ssh/targets.yaml` through `load_ssh_targets()` | None | None; does not create terminal environments or change bindings | Must not read private key contents or implicit-import `~/.ssh/config` |
| `/ssh status` | `~/.hermes/ssh/bindings.json`; target registry only to resolve the current alias | None | None; reports backend `local` when no binding exists, `ssh` when the section key resolves | Must not change binding or create an SSH environment |
| `/ssh test <alias>` | Target registry | None | Validates that target metadata is complete; no connection is required by the command itself | Must not change binding or yolo grant |
| `/ssh use <alias> [--cwd <remote-path>]` inside an existing section/thread | Target registry; current section key | `~/.hermes/ssh/bindings.json` via `set_ssh_binding()` | Registers section-scoped terminal overrides under the durable session key, evicts cached agent for that section, and causes the next turn's prompt plus terminal/file/code tools to use backend `ssh` | Must not modify `~/.ssh/config`; must not read private key contents; must not affect other sections |
| `/ssh use <alias> [--cwd <remote-path>]` from a Feishu parent chat | Target registry; parent message metadata | `~/.hermes/ssh/bindings.json` for the newly created thread's section key | Creates a Feishu thread by default, binds that thread to SSH, and leaves the parent chat unbound | Must not require `-t`; must not bind the parent chat silently |
| `/ssh use <alias> -t` / `--thread` | Same as parent-chat default use | Same as parent-chat default use | Explicit form of the default parent-chat behavior | Must not create different semantics from plain parent-chat `/ssh use` |
| `/ssh local` | Current section key | `~/.hermes/ssh/bindings.json` via `clear_ssh_binding()` | Clears section-scoped terminal override, evicts cached agent, and returns future prompt/tool execution to local/session default backend | Must not delete targets from the registry |
| `/ssh off` | Current section key | Same as `/ssh local` | Compatibility alias for `/ssh local` | Must not be documented as the primary exit command |
| `/ssh yolo status` | `~/.hermes/ssh/bindings.json` yolo grant section | None | None | Must not switch backend |
| `/ssh yolo on [alias|all]` | Target registry when an alias is provided; yolo grant section | `~/.hermes/ssh/bindings.json` yolo grant section | Allows model-initiated SSH switching only for the current section and configured aliases | Must not grant globally across sessions unless the section key itself is shared |
| `/ssh yolo off [alias]` | Yolo grant section | `~/.hermes/ssh/bindings.json` yolo grant section | Removes one alias grant or disables section yolo entirely | Must not clear the SSH binding itself |

Runtime note: system-level SSH Mode is `terminal.backend=ssh` plus `TERMINAL_SSH_*` config. Section-level SSH Mode must be equivalent from the model/tool perspective: `build_environment_hints()` should report `Terminal backend: ssh`, and `terminal`, `read_file`/`write_file`/`patch`/`search_files`, and `execute_code` should all resolve the same SSH environment for the section until `/ssh local` clears it.

## SSH Mode Test Strategy

A complete SSH Mode change needs three kinds of validation:

1. **Interaction mock tests**: simulate Feishu parent chat and thread events. Assert `/ssh use <alias>` without `-t` creates a thread by default, writes exactly the new thread binding, and does not require the user to remember a flag. Also simulate an existing Feishu thread and assert `/ssh use <other-alias>` rewrites that thread's binding without touching the parent chat or other threads.
2. **Backend-equivalence mock tests**: register section-scoped SSH overrides and assert prompt building plus terminal/file/code tools behave as if system-level `terminal.backend=ssh` were active. This must include: model environment hints reporting `Terminal backend: ssh`; terminal commands creating an SSH env; file tools using that same env for read/write/search/patch; `execute_code` using that same env; and `/ssh local` returning all future work to local/session-default behavior.
3. **Live read-only smoke**: when a permitted target exists, run a short `pwd && whoami && hostname` or `printf ok` through the Hermes terminal SSH backend. Use raw `ssh` only as a control, not as proof that Hermes tool routing works.

Minimum acceptance scenarios:

- **From scratch in a Feishu parent chat**: `/ssh use <alias>` creates a thread, writes only the new thread's binding, returns a message that says future tools in that thread use SSH, and subsequent work in the parent chat remains local.
- **Inside an existing Feishu thread**: `/ssh use <alias>` updates the current thread binding; `/ssh status` reports backend `ssh`; the next prompt build and terminal/file/code tools all see SSH.
- **Switching targets inside a thread**: a second `/ssh use <other-alias>` replaces the binding, refreshes runtime overrides, evicts the cached agent for that section, and future tools use the new target.
- **Return to local**: `/ssh local` clears the binding, clears runtime overrides, evicts the cached agent, and `/ssh status` plus future tools return to local/session-default behavior. `/ssh off` may do the same only as a compatibility alias.

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
5. **Skipping `/ssh local`.** Always verify that returning to local mode clears the section binding. Treat `/ssh off` only as a compatibility alias.

## Verification Checklist

- [ ] Workspace and current profile are confirmed.
- [ ] Target selection is explicit.
- [ ] Private key contents were not read.
- [ ] Identity paths are redacted in user-visible output.
- [ ] `load_ssh_targets()` and `render_ssh_targets()` pass.
- [ ] `validate_ssh_target_for_runtime()` reports no missing host/user.
- [ ] `ssh -G <alias>` resolves expected host/user/port.
- [ ] Read-only smoke checks were run when appropriate.
- [ ] `/ssh list/status/test/use/local/status` flow was verified.
- [ ] Any repo-bundled change is on a branch and contains no private target list.
