"""Invariant for registry-owned slash execution (CommandDef.execute).

Every ``CommandDef`` with ``execute`` set must name a key that exists in
:data:`hermes_cli.slash_exec.EXECUTORS`; otherwise the command silently falls
through to "unknown command" on every surface.
"""

import pytest

from hermes_cli.commands import COMMAND_REGISTRY, resolve_command
from hermes_cli.slash_exec import (
    EXECUTORS,
    CommandContext,
    CommandReply,
    execute_command,
    resolve_executor,
    run_execute,
)

MIGRATED = [cmd for cmd in COMMAND_REGISTRY if cmd.execute]

SURFACES = ("cli", "gateway", "tui")


def test_some_commands_are_migrated():
    names = {cmd.name for cmd in MIGRATED}
    # The thin-slice set — extend as more commands migrate.
    assert {"version", "egress", "profile", "bundles", "help", "commands", "cron"} <= names




def test_unmigrated_commands_have_no_executor():
    for cmd in COMMAND_REGISTRY:
        if not cmd.execute:
            assert resolve_executor(cmd) is None
            assert run_execute(cmd, CommandContext()) is None










def test_every_execute_key_resolves_to_an_executor():
    migrated = [cmd for cmd in COMMAND_REGISTRY if cmd.execute]
    assert migrated
    unresolved = [cmd.name for cmd in migrated if resolve_executor(cmd) is None]
    assert not unresolved, f"CommandDef.execute names no EXECUTORS entry: {unresolved}"
