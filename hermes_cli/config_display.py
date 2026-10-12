"""Human-facing config display; read runtime state through the config facade.

The public ``config.show_config`` entry remains on the facade. Helpers here
late-bind its getters so profile scope and existing patch seams stay live.
"""

from typing import Any

_SHOW_CONFIG_API_KEYS = (
    ("OPENROUTER_API_KEY", "OpenRouter"),
    ("VOICE_TOOLS_OPENAI_KEY", "OpenAI (STT/TTS)"),
    ("EXA_API_KEY", "Exa"),
    ("PARALLEL_API_KEY", "Parallel"),
    ("FIRECRAWL_API_KEY", "Firecrawl"),
    ("TAVILY_API_KEY", "Tavily"),
    ("PERPLEXITY_API_KEY", "Perplexity"),
    ("BROWSERBASE_API_KEY", "Browserbase"),
    ("BROWSER_USE_API_KEY", "Browser Use"),
    ("FAL_KEY", "FAL"))


def _show_managed_banner() -> None:
    """Surface administrator-pinned settings so the user knows why a config.yaml value may not
    be the effective one."""
    from hermes_cli.config import (managed_scope, color, Colors)
    managed_keys = managed_scope.managed_config_keys()
    managed_env = managed_scope.load_managed_env()
    if not managed_keys and not managed_env:
        return
    print()
    print(color(
        f"  ⚷ Some settings are managed by your administrator ({managed_scope.get_managed_dir()}) "
        f"and cannot be changed", Colors.YELLOW, Colors.BOLD))
    for label, keys in (("config", managed_keys), ("env", managed_env)):
        if keys:
            print(color(f"    Managed {label} keys: {', '.join(sorted(keys))}", Colors.YELLOW))


def _show_terminal_section(config: dict[str, Any]) -> None:
    from hermes_cli.config import _section, get_env_value

    _section("Terminal")
    terminal = config.get('terminal', {})
    print(f"  Backend:      {terminal.get('backend', 'local')}")
    print(f"  Working dir:  {terminal.get('cwd', '.')}")
    print(f"  Timeout:      {terminal.get('timeout', 60)}s")

    configured = lambda *names: 'configured' if all(get_env_value(n) for n in names) else '(not set)'
    from hermes_cli.config_defaults import DEFAULT_SANDBOX_IMAGE as default_img, DEFAULT_VERCEL_IMAGE as _DEFAULT_VERCEL_IMAGE
    backend_lines = {
        'docker': lambda: [f"  Docker image: {terminal.get('docker_image', default_img)}"],
        'singularity': lambda: [f"  Image:        {terminal.get('singularity_image', 'docker://' + default_img)}"],
        'modal': lambda: [
            f"  Modal image:  {terminal.get('modal_image', default_img)}",
            f"  Modal token:  {configured('MODAL_TOKEN_ID')}"],
        'daytona': lambda: [
            f"  Daytona image: {terminal.get('daytona_image', default_img)}",
            f"  API key:      {configured('DAYTONA_API_KEY')}"],
        'vercel_sandbox': lambda: [
            f"  Vercel image:   {terminal.get('vercel_runtime') or terminal.get('vercel_image') or _DEFAULT_VERCEL_IMAGE}",
            f"  Vercel auth:    {'configured' if get_env_value('VERCEL_OIDC_TOKEN') or (get_env_value('VERCEL_TOKEN') and get_env_value('VERCEL_PROJECT_ID') and get_env_value('VERCEL_TEAM_ID')) else '(not set)'}",
        ],
        'ssh': lambda: [
            f"  SSH host:     {get_env_value('TERMINAL_SSH_HOST') or '(not set)'}",
            f"  SSH user:     {get_env_value('TERMINAL_SSH_USER') or '(not set)'}"]}
    for line in backend_lines.get(terminal.get('backend'), list)():
        print(line)


def _show_compression_section(config: dict[str, Any]) -> None:
    from hermes_cli.config import _section

    _section("Context Compression")
    compression = config.get('compression', {})
    enabled = compression.get('enabled', True)
    print(f"  Enabled:      {'yes' if enabled else 'no'}")
    if not enabled:
        return
    print(f"  Threshold:    {compression.get('threshold', 0.50) * 100:.0f}%")
    tt = compression.get('threshold_tokens')
    try:
        if tt is not None and int(tt) > 0:
            print(f"  Token cap:    {int(tt):,} tokens (takes lower of ratio vs absolute)")
    except (TypeError, ValueError):
        pass
    print(f"  Target ratio: {compression.get('target_ratio', 0.20) * 100:.0f}% of threshold preserved")
    print(f"  Protect last: {compression.get('protect_last_n', 20)} messages")
    print(f"  Protect first: {compression.get('protect_first_n', 3)} non-system head messages")
    aux_comp = config.get('auxiliary', {}).get('compression', {})
    print(f"  Model:        {aux_comp.get('model', '') or '(auto)'}")
    comp_provider = aux_comp.get('provider', 'auto')
    if comp_provider and comp_provider != 'auto':
        print(f"  Provider:     {comp_provider}")


def _show_aux_overrides(config: dict[str, Any]) -> None:
    from hermes_cli.config import _section

    aux_tasks = {"Vision": config.get('auxiliary', {}).get('vision', {})}
    overrides = {
        label: (t.get('provider', 'auto'), t.get('model', ''))
        for label, t in aux_tasks.items()
        if t.get('provider', 'auto') != 'auto' or t.get('model', '')}
    if not overrides:
        return
    _section("Auxiliary Models (overrides)")
    for label, (prov, mdl) in overrides.items():
        parts = [f"provider={prov}"] + ([f"model={mdl}"] if mdl else [])
        print(f"  {label:12s}  {', '.join(parts)}")


def show_config():
    """Display current configuration."""
    from hermes_cli.config import (
        Colors, _section, _show_display_section, _show_model_section, _show_skill_settings,
        color, get_config_path, get_env_path, get_env_value, get_project_root, load_config, redact_key,
    )
    config = load_config()

    print()
    print(color("┌─────────────────────────────────────────────────────────┐", Colors.CYAN))
    print(color("│              ☤ Hermes Configuration                    │", Colors.CYAN))
    print(color("└─────────────────────────────────────────────────────────┘", Colors.CYAN))
    _show_managed_banner()

    _section("Paths")
    print(f"  Config:       {get_config_path()}")
    print(f"  Secrets:      {get_env_path()}")
    print(f"  Install:      {get_project_root()}")

    _section("API Keys")
    for env_key, name in _SHOW_CONFIG_API_KEYS:
        print(f"  {name:<14} {redact_key(get_env_value(env_key))}")
    from hermes_cli.auth import get_anthropic_key
    print(f"  {'Anthropic':<14} {redact_key(get_anthropic_key())}")

    _show_model_section(config)
    _show_display_section(config)
    _show_terminal_section(config)

    _section("Timezone")
    tz = config.get('timezone', '')
    print(f"  Timezone:     {tz or color('(server-local)', Colors.DIM)}")

    _show_compression_section(config)
    _show_aux_overrides(config)

    _section("Messaging Platforms")
    for label, env_key in (("Telegram", "TELEGRAM_BOT_TOKEN"), ("Discord", "DISCORD_BOT_TOKEN")):
        state = 'configured' if get_env_value(env_key) else color('not configured', Colors.DIM)
        print(f"  {label + ':':<13} {state}")

    _show_skill_settings()

    print()
    print(color("─" * 60, Colors.DIM))
    print(color("  hermes config edit     # Edit config file", Colors.DIM))
    print(color("  hermes config set <key> <value>", Colors.DIM))
    print(color("  hermes setup           # Run setup wizard", Colors.DIM))
    print()
