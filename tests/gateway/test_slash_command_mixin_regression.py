"""Regression tests for slash-command mixin extraction staying authoritative."""

from gateway.run import GatewayRunner
from gateway.slash_commands import GatewaySlashCommandsMixin


def test_gateway_runner_uses_slash_mixin_reset_handler():
    """GatewayRunner must not shadow the extracted /new handler.

    CubeNLP main carries the current /new implementation in
    GatewaySlashCommandsMixin. Reintroducing an older copy in gateway.run during
    a feature merge silently bypasses fixes in the mixin, including running-agent
    slot release during reset.
    """

    assert GatewayRunner._handle_reset_command is GatewaySlashCommandsMixin._handle_reset_command


def test_gateway_runner_uses_slash_mixin_profile_handler():
    """GatewayRunner must not shadow the extracted /profile handler."""

    assert GatewayRunner._handle_profile_command is GatewaySlashCommandsMixin._handle_profile_command
