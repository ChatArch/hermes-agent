from __future__ import annotations
import asyncio
import dataclasses
import logging
import re
from pathlib import Path
from typing import Any, Optional
from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource
from hermes_constants import get_hermes_home
from gateway.ssh_targets import find_ssh_target, load_ssh_targets, render_ssh_targets, validate_ssh_target_for_runtime
from gateway.ssh_bindings import (LOCAL_BACKEND, clear_ssh_binding, get_backend_auto_policy,
    get_ssh_binding, is_local_backend, list_backend_auto_policies, normalize_backend_name,
    resolve_binding_task_overrides, resolve_binding_target, set_backend_auto_enabled, set_ssh_binding)
logger = logging.getLogger("gateway.run")

class ChatArchGatewayMixin:
    async def _handle_local_command_during_drain(self, event: MessageEvent):
        return f"⏳ Gateway is {self._status_action_gerund()} and is not accepting new work right now."

    def _template_usage(self) -> str:
        return "Usage: /template <name|list|create|update|use> [instruction...]"

    def _parse_template_args(self, raw_args: str) -> tuple[str | None, str | None, str]:
        """Parse `/template` arguments into (action, name, instruction)."""
        raw_args = (raw_args or "").strip()
        if not raw_args:
            return None, None, ""
        first, sep, rest = raw_args.partition(" ")
        first_norm = first.strip().lower()
        if first_norm in {"list", "ls"}:
            return "list", None, rest.strip()
        if first_norm in {"create", "new", "update", "edit", "use", "run"}:
            rest = rest.strip()
            if not rest:
                return first_norm, None, ""
            name, _sep2, instruction = rest.partition(" ")
            action = {
                "new": "create",
                "edit": "update",
                "run": "use",
            }.get(first_norm, first_norm)
            return action, name.strip(), instruction.strip()
        return "use", first.strip(), rest.strip() if sep else ""

    def _template_store_dir(self) -> Path:
        """Return the private template store root.

        Templates deliberately live outside ``~/.hermes/skills`` so they do not
        become dynamic ``/<skill-name>`` slash commands or participate in the
        official Hermes skill lifecycle.
        """
        return get_hermes_home() / "templates"

    def _template_path(self, name: str) -> Path | None:
        name = (name or "").strip().lower().replace("_", "-")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name):
            return None
        return self._template_store_dir() / name / "SKILL.md"

    def _list_template_names(self) -> list[str]:
        root = self._template_store_dir()
        if not root.exists():
            return []
        out: list[str] = []
        try:
            for child in sorted(root.iterdir()):
                template_path = self._template_path(child.name)
                if not child.is_dir() or template_path is None:
                    continue
                if template_path == child / "SKILL.md" and template_path.is_file():
                    out.append(child.name)
        except OSError:
            return []
        return out

    def _template_list_message(self) -> str:
        names = self._list_template_names()
        if not names:
            return (
                "No templates found.\n"
                "Use `/template create <name> <what this template should do>` to create one."
            )

        lines = ["Available templates:"]
        for name in names:
            description = ""
            template_path = self._template_path(name)
            if template_path is not None:
                try:
                    content = template_path.read_text(encoding="utf-8")
                except OSError:
                    content = ""
                if content:
                    meta, _body = self._parse_template_frontmatter(content)
                    description = str(meta.get("description") or "").strip()
            if description:
                lines.append(f"- `{name}` — {description}")
            else:
                lines.append(f"- `{name}`")
        lines.extend(
            [
                "",
                "Use `/template <name> [instruction...]` to run one in a Feishu thread.",
                "Use `/template create <name> <instruction...>` to add a new template.",
            ]
        )
        return "\n".join(lines)

    def _parse_template_frontmatter(self, content: str) -> tuple[dict[str, Any], str]:
        if not content.startswith("---"):
            return {}, content.strip()
        lines = content.splitlines()
        end_idx = None
        for idx, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                end_idx = idx
                break
        if end_idx is None:
            return {}, content.strip()
        meta: dict[str, Any] = {}
        for line in lines[1:end_idx]:
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip().strip('"\'')
        body = "\n".join(lines[end_idx + 1 :]).strip()
        return meta, body

    def _build_template_use_payload(self, *, name: str, instruction: str) -> str | None:
        template_path = self._template_path(name)
        if template_path is None or not template_path.is_file():
            return None
        try:
            content = template_path.read_text(encoding="utf-8")
        except OSError:
            return None
        meta, body = self._parse_template_frontmatter(content)
        display_name = str(meta.get("name") or name)
        description = str(meta.get("description") or "")
        parts = [
            f'[IMPORTANT: The user has invoked the "{display_name}" template. Treat the template below as active guidance for this thread.]',
            "",
            f"Template: {display_name}",
        ]
        if description:
            parts.append(f"Description: {description}")
        parts.extend(
            [
                f"Template file: {template_path}",
                "",
                body or content.strip(),
            ]
        )
        if instruction:
            parts.extend(["", f"User instruction: {instruction}"])
        parts.extend(["", "[Runtime note: thread-isolated /template use invocation]"])
        return "\n".join(parts)

    def _template_management_prompt(self, *, action: str, name: str, instruction: str) -> str:
        template_path = self._template_path(name)
        target = template_path or (self._template_store_dir() / name / "SKILL.md")
        action_title = (
            "Create a new Hermes template"
            if action == "create"
            else "Update an existing Hermes template"
        )
        return "\n".join(
            [
                action_title,
                "",
                f"User requested template name: {name}",
                f"Template file: {target}",
                "",
                "Maintain this as a private template, not as an official Hermes Skill.",
                "Use SKILL.md format: YAML frontmatter with at least `name` and `description`, then a clear reusable instruction body.",
                "Keep it under the private template store (`~/.hermes/templates/<name>/SKILL.md`) so it does not become a dynamic `/<skill-name>` command.",
                "Create parent directories when needed. Prefer targeted edits when updating an existing template.",
                "",
                f"User instruction: {instruction or '(no extra instruction provided)'}",
            ]
        )

    def _template_unknown_target_message(self, name: str) -> str:
        available = self._list_template_names()
        hint = ""
        if available:
            hint = "\nAvailable templates: " + ", ".join(f"`{item}`" for item in available[:12])
        return (
            f"Unknown template `{name}`.\n"
            f"Use `/template create {name} <what this template should do>` to create it."
            f"{hint}"
        )

    def _template_invalid_name_message(self, name: str) -> str:
        return (
            f"Invalid template name `{name}`. Use 1-64 lowercase letters, numbers, "
            "and hyphens only, starting with a letter or number."
        )

    async def _dispatch_event_in_feishu_thread(
        self,
        event: MessageEvent,
        payload: str,
        *,
        command_name: str,
        reply_text: str,
        reset_existing_thread: bool = False,
        invalidation_reason: str | None = None,
    ) -> Any:
        """Dispatch ``payload`` as a Feishu-thread-scoped agent turn.

        This shared helper owns the common thread launcher sequence used by
        `/thread` and thread-oriented commands: create Feishu thread when the
        command starts in a parent chat, retarget the event/source, dispatch the
        agent turn, retract the temporary seed message, and return the final
        answer for normal thread-bottom delivery.
        """
        from gateway.run import _INTERRUPT_REASON_RESET

        source = event.source
        original_session_key = self._session_key_for_source(source)
        adapter = self.adapters.get(source.platform)
        thread_source = source
        original_message_id = getattr(event, "message_id", None)
        reply_anchor_message_id = getattr(event, "reply_to_message_id", None) or original_message_id
        seed_message_id = original_message_id
        created_thread_seed_message_id = None

        if source.thread_id:
            if reset_existing_thread:
                thread_key = self._session_key_for_source(source)
                if thread_key in self._running_agents:
                    await self._interrupt_and_clear_session(
                        thread_key,
                        source,
                        interrupt_reason=_INTERRUPT_REASON_RESET,
                        invalidation_reason=invalidation_reason or command_name,
                    )
                reset_event = dataclasses.replace(event, text="/new")
                await self._handle_reset_command(reset_event)
        else:
            create_thread = getattr(adapter, "create_thread", None) if adapter else None
            if create_thread is None:
                return f"Feishu /{command_name} is unavailable: adapter does not support thread creation."
            if not seed_message_id:
                return f"Feishu /{command_name} requires a message id to create a thread."
            result = await create_thread(
                source.chat_id,
                "⏳",
                reply_to=str(seed_message_id),
            )
            if not getattr(result, "success", False):
                return f"Failed to create Feishu thread: {getattr(result, 'error', 'unknown error')}"
            thread_id = getattr(result, "thread_id", None) or getattr(result, "message_id", None)
            if not thread_id:
                return "Failed to create Feishu thread: response did not include a thread id."
            seed_message_id = getattr(result, "message_id", None) or str(seed_message_id)
            created_thread_seed_message_id = seed_message_id
            thread_source = dataclasses.replace(
                source,
                thread_id=str(thread_id),
                parent_chat_id=source.chat_id,
                message_id=original_message_id,
            )
            release_retargeted = getattr(adapter, "release_retargeted_session_guard", None)
            if release_retargeted is not None:
                release_retargeted(original_session_key)

        event.text = payload
        event.message_type = MessageType.TEXT
        event.source = thread_source
        event.reply_to_message_id = reply_anchor_message_id
        event.reply_to_text = reply_text

        thread_key = self._session_key_for_source(thread_source)
        agent_result = await self._dispatch_event_to_agent(event, thread_source, thread_key)

        if created_thread_seed_message_id:
            delete_message = getattr(adapter, "delete_message", None) if adapter else None
            if delete_message is not None:
                try:
                    deleted = await delete_message(source.chat_id, str(created_thread_seed_message_id))
                except Exception as exc:
                    deleted = False
                    logger.warning(
                        "Feishu /%s seed delete failed for %s: %s",
                        command_name,
                        created_thread_seed_message_id,
                        exc,
                        exc_info=True,
                    )
                if not deleted:
                    logger.warning(
                        "Feishu /%s seed delete failed for %s",
                        command_name,
                        created_thread_seed_message_id,
                    )
            return agent_result

        return agent_result

    async def _handle_template_command(self, event: MessageEvent) -> Any:
        """Handle Feishu `/template` thread launcher."""
        source = event.source
        if source.platform != Platform.FEISHU:
            return "/template is currently supported only on Feishu."

        action, name, instruction = self._parse_template_args(event.get_command_args())
        if not action:
            return self._template_usage()
        if action == "list":
            return self._template_list_message()
        if not name:
            return self._template_usage()
        if self._template_path(name) is None:
            return self._template_invalid_name_message(name)

        if source.thread_id:
            thread_key = self._session_key_for_source(source)
            if thread_key in self._running_agents:
                return (
                    "⏳ Agent is running in this thread — `/template` can't start "
                    "another turn here. Wait for the current response or `/stop` first."
                )

        if action in {"create", "update"}:
            payload = self._template_management_prompt(
                action=action,
                name=name,
                instruction=instruction,
            )
        elif action == "use":
            payload = self._build_template_use_payload(name=name, instruction=instruction)
            if not payload:
                return self._template_unknown_target_message(name)
        else:
            return self._template_usage()

        return await self._dispatch_event_in_feishu_thread(
            event,
            payload,
            command_name="template",
            reply_text=instruction or name,
            reset_existing_thread=False,
        )

    async def _handle_ssh_command(self, event: MessageEvent) -> str:
        """Handle gateway `/ssh` section backend commands."""

        raw_args = event.get_command_args().strip()
        parts = raw_args.split()
        action = parts[0].lower() if parts else "help"

        usage = (
            "Usage:\n"
            "/ssh list — list local plus configured SSH backends\n"
            "/ssh status — show this section's current backend and auto-switch policy\n"
            "/ssh test <backend> — validate local or an SSH backend without switching\n"
            "/ssh use <backend> [--cwd <remote-path>] — explicitly switch current backend; backend can be local\n"
            "/ssh on <backend|all> — allow model-initiated switching to that backend, or every backend\n"
            "/ssh off <backend|all> — require approval before model-initiated switching to that backend, or every backend"
        )

        if action in {"help", ""}:
            return usage

        section_key = self._session_key_for_source(event.source)

        def _backend_arg() -> str:
            return normalize_backend_name(parts[1]) if len(parts) > 1 else ""

        def _target_for_backend(backend: str):
            if is_local_backend(backend):
                return None
            return find_ssh_target(load_ssh_targets(), backend)

        def _known_backend_names() -> list[str]:
            return [LOCAL_BACKEND, *[target.alias for target in load_ssh_targets()]]

        def _format_auto(value: bool) -> str:
            return "on" if value else "off"

        def _render_backend_lines() -> list[str]:
            resolved = resolve_binding_target(section_key, targets=load_ssh_targets())
            current = resolved[0].alias if resolved else LOCAL_BACKEND
            backends = _known_backend_names()
            policy = list_backend_auto_policies(section_key, backends)
            lines = ["SSH backends:"]
            local_marks = []
            if current == LOCAL_BACKEND:
                local_marks.append("current")
            local_marks.append(f"auto:{_format_auto(policy.get(LOCAL_BACKEND, True))}")
            lines.append(f"- `{LOCAL_BACKEND}` (local; {', '.join(local_marks)})")
            for target in load_ssh_targets():
                marks = []
                if current == target.alias:
                    marks.append("current")
                marks.append(f"auto:{_format_auto(policy.get(target.alias, False))}")
                details = []
                if target.host:
                    details.append(f"host={target.host}")
                if target.user:
                    details.append(f"user={target.user}")
                if target.port:
                    details.append(f"port={target.port}")
                if target.cwd:
                    details.append(f"cwd={target.cwd}")
                if target.identity_file:
                    details.append("identity=[REDACTED_PATH]")
                suffix = f" — {'; '.join(details)}" if details else ""
                lines.append(f"- `{target.alias}` (ssh; {', '.join(marks)}){suffix}")
            return lines

        if action == "list":
            return "\n".join(_render_backend_lines())

        if action == "status":
            resolved = resolve_binding_target(section_key, targets=load_ssh_targets())
            current = resolved[0].alias if resolved else LOCAL_BACKEND
            policy = list_backend_auto_policies(section_key, _known_backend_names())
            lines = [
                "SSH status:",
                f"- current backend: `{current}`",
                f"- backend type: {'local' if current == LOCAL_BACKEND else 'ssh'}",
                f"- section key: {section_key}",
                "- auto-switch:",
            ]
            for backend in _known_backend_names():
                lines.append(f"  - `{backend}`: {_format_auto(policy.get(backend, is_local_backend(backend)))}")
            if resolved is not None:
                binding, target = resolved
                lines.extend([
                    f"- binding source: {binding.source}",
                ])
                if target.host:
                    lines.append(f"- host: {target.host}")
                if target.user:
                    lines.append(f"- user: {target.user}")
                if target.port:
                    lines.append(f"- port: {target.port}")
                cwd = binding.cwd or target.cwd
                if cwd:
                    lines.append(f"- cwd: {cwd}")
                if target.identity_file:
                    lines.append("- identity: [REDACTED_PATH]")
            return "\n".join(lines)

        if action == "test":
            backend = _backend_arg()
            if not backend:
                return "Usage: /ssh test <backend>"
            if is_local_backend(backend):
                return "SSH test: `local` backend is available."
            target = _target_for_backend(backend)
            if target is None:
                return f"Unknown backend: `{backend}`. Use /ssh list to see backends."
            target_error = validate_ssh_target_for_runtime(target)
            if target_error:
                return target_error
            return f"SSH test: `{backend}` is configured. No binding was changed."

        if action in {"on", "off"}:
            backend = _backend_arg()
            if not backend:
                return f"Usage: /ssh {action} <backend|all>"
            if backend.lower() == "all":
                enabled = action == "on"
                backends = _known_backend_names()
                failed: list[str] = []
                for name in backends:
                    update = set_backend_auto_enabled(section_key, name, enabled)
                    if not update.ok:
                        failed.append(name)
                if failed:
                    return f"Failed to update backend auto-switch policy for: {', '.join(f'`{name}`' for name in failed)}"
                state = "enabled" if enabled else "disabled"
                effect = "allowed" if enabled else "will require approval"
                suffix = "" if enabled else " current backend remains unchanged."
                return (
                    f"All backend auto-switch policies {state} for this section: "
                    + ", ".join(f"`{name}`" for name in backends)
                    + f". Model-initiated use {effect}."
                    + suffix
                )
            if not is_local_backend(backend) and _target_for_backend(backend) is None:
                return f"Unknown backend: `{backend}`. Use /ssh list to see backends."
            update = set_backend_auto_enabled(section_key, backend, action == "on")
            if not update.ok:
                return update.message or f"Backend `{backend}` auto-switch policy was not changed."
            state = "enabled" if update.enabled else "disabled"
            effect = "allowed" if update.enabled else "will require approval"
            return f"Backend `{backend}` auto-switch {state} for this section; model-initiated use {effect}."

        if action == "use":
            backend = _backend_arg()
            if not backend:
                return "Usage: /ssh use <backend> [--cwd <remote-path>]"
            if is_local_backend(backend):
                clear_ssh_binding(section_key)
                try:
                    from tools.terminal_tool import clear_task_env_overrides
                    clear_task_env_overrides(section_key)
                except Exception:
                    pass
                self._evict_cached_agent(section_key)
                return "Backend switched for this section: `local`."

            target = _target_for_backend(backend)
            if target is None:
                return f"Unknown backend: `{backend}`. No binding was changed. Use /ssh list to see backends."
            target_error = validate_ssh_target_for_runtime(target)
            if target_error:
                return target_error
            cwd = None
            if "--cwd" in parts:
                idx = parts.index("--cwd")
                if idx + 1 >= len(parts):
                    return "Usage: /ssh use <backend> --cwd <remote-path>"
                cwd = parts[idx + 1]

            source = event.source
            bind_source = source
            if not source.thread_id:
                if source.platform != Platform.FEISHU:
                    return "Please run `/ssh use <backend>` inside a thread/section that supports backend bindings."
                adapter = self.adapters.get(source.platform)
                create_thread = getattr(adapter, "create_thread", None) if adapter else None
                if create_thread is None:
                    return "Feishu /ssh use is unavailable: adapter does not support thread creation."
                if not getattr(event, "message_id", None):
                    return "Feishu /ssh use requires a message id to create a thread."
                result = await create_thread(source.chat_id, "SSH backend binding", reply_to=str(event.message_id))
                if not getattr(result, "success", False):
                    return f"Failed to create Feishu thread: {getattr(result, 'error', 'unknown error')}"
                thread_id = getattr(result, "thread_id", None) or getattr(result, "message_id", None)
                if not thread_id:
                    return "Failed to create Feishu thread: response did not include a thread id."
                bind_source = dataclasses.replace(
                    source,
                    thread_id=str(thread_id),
                    parent_chat_id=source.chat_id,
                    message_id=getattr(result, "message_id", None) or event.message_id,
                )

            bind_key = self._session_key_for_source(bind_source)
            binding = set_ssh_binding(bind_key, alias=backend, cwd=cwd, source="user")
            overrides = resolve_binding_task_overrides(bind_key, targets=load_ssh_targets())
            if overrides:
                try:
                    from tools.terminal_tool import register_task_env_overrides
                    register_task_env_overrides(bind_key, overrides)
                except Exception:
                    pass
            self._evict_cached_agent(bind_key)
            lines = [
                f"Backend switched for this section: `{binding.alias}`",
                "- backend type: ssh",
                f"- section key: {bind_key}",
            ]
            effective_cwd = binding.cwd or target.cwd
            if effective_cwd:
                lines.append(f"- cwd: {effective_cwd}")
            if target.identity_file:
                lines.append("- identity: [REDACTED_PATH]")
            lines.append("Future terminal/file/execute_code calls in this section will use this backend.")
            return "\n".join(lines)

        return usage

    async def _handle_thread_command(self, event: MessageEvent):
        """Handle Feishu /thread <prompt>.

        In a normal Feishu chat this creates a new Feishu thread from the
        command message, then runs the prompt as a thread-scoped Hermes turn. In
        an existing Feishu thread it resets that thread's Hermes session and
        runs the prompt in the same thread.
        """
        source = event.source
        if source.platform != Platform.FEISHU:
            return "/thread is currently supported only on Feishu."

        prompt = event.get_command_args().strip()
        if not prompt:
            return "Usage: /thread <prompt>"

        return await self._dispatch_event_in_feishu_thread(
            event,
            prompt,
            command_name="thread",
            reply_text=prompt,
            reset_existing_thread=True,
            invalidation_reason="thread_command",
        )

    async def _busy_interrupt_command(self, event: MessageEvent, quick_key: str, source):
        """Apply ChatArch's explicit one-shot interrupt semantics mid-run."""
        from gateway.run import _AGENT_PENDING_SENTINEL
        interrupt_text = event.get_command_args().strip()
        if not interrupt_text:
            return "Usage: /interrupt <prompt>"

        state = self._peek_session_state(quick_key)
        running_agent = state.turn.agent if state else None
        if running_agent is _AGENT_PENDING_SENTINEL:
            running_agent = None

        interrupt_method = getattr(running_agent, "interrupt", None)
        if not running_agent or not callable(interrupt_method):
            adapter = self._adapter_for_source(source)
            if adapter:
                queued_event = MessageEvent(
                    text=interrupt_text,
                    message_type=MessageType.TEXT,
                    source=event.source,
                    message_id=event.message_id,
                    channel_prompt=event.channel_prompt,
                    channel_context=event.channel_context,
                )
                self._enqueue_fifo(quick_key, queued_event, adapter)
            return "Agent is not interruptible yet — /interrupt payload queued for the next turn."

        try:
            interrupt_method(interrupt_text)
        except Exception as exc:
            logger.warning("Interrupt failed for session %s: %s", quick_key, exc)
            return "⚠️ Interrupt failed. The current run was not interrupted."
        preview_source = " ".join(interrupt_text.split())
        preview = preview_source[:60] + ("..." if len(preview_source) > 60 else "")
        return f"⚡ Interrupted current run: '{preview}'"

    @staticmethod
    def _remote_media_failure_response(response: str) -> str:
        """Return user-visible text for a failed remote MEDIA rewrite.

        ``MEDIA:file://`` / ``MEDIA:ssh://`` directives are internal transport
        controls. If the materializer itself errors before it can rewrite or
        remove them, fail closed: keep the explanatory text, drop every real
        MEDIA directive, and add an explicit attachment failure notice instead
        of leaking the raw directive into chat.
        """
        from gateway.platforms.base import BasePlatformAdapter

        notice = "⚠️ One or more remote attachments could not be retrieved."
        cleaned = BasePlatformAdapter.strip_media_directives_for_display(
            response or ""
        ).strip()
        if not cleaned:
            return notice
        if notice in cleaned:
            return cleaned
        return f"{cleaned}\n\n{notice}"

    @staticmethod
    def _queued_followup_first_response_for_direct_send(response: str) -> str:
        """Sanitize queued-turn direct sends that bypass normal media delivery.

        The queued-follow-up branch sends a completed response before recursing
        into the user's next pending message. That branch uses ``adapter.send()``
        directly, so it intentionally bypasses the normal runner/platform
        response pipeline that materializes remote MEDIA directives and strips
        internal transport markers. If a remote MEDIA directive reaches this
        path, fail closed before sending the text bubble.
        """
        if not response:
            return response
        try:
            from gateway.platforms.base import MEDIA_RESOURCE_URI_RE

            if MEDIA_RESOURCE_URI_RE.search(response):
                return ChatArchGatewayMixin._remote_media_failure_response(response)
        except Exception:
            # If the detector itself fails, keep the user-facing output safe.
            return ChatArchGatewayMixin._remote_media_failure_response(response)
        return response

    async def _materialize_media_for_delivery(
        self,
        response: str,
        *,
        session_id: str,
        fallback_session_id: Optional[str] = None,
        session_key: str,
    ):
        """Resolve file/SSH MEDIA refs before platform adapters inspect them."""
        from gateway.media_materializer import materialize_response_media

        binding = get_ssh_binding(session_key or "")
        return await asyncio.to_thread(
            materialize_response_media,
            response,
            task_id=session_id,
            fallback_task_ids=tuple(
                candidate
                for candidate in (fallback_session_id, session_key)
                if candidate
            ),
            ssh_alias=binding.alias if binding is not None else None,
        )
