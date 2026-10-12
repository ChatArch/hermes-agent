"""The bootstrap parser stays usable in a stdlib-only recovery checkout."""

from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from hermes_cli import _parser


@pytest.mark.parametrize("operation", ["bootstrap", "hint"])
def test_parser_works_without_the_plugin_diagnostics_package(tmp_path, operation):
    package = tmp_path / "hermes_cli"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    shutil.copyfile(Path(_parser.__file__), package / "_parser.py")
    script = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from hermes_cli._parser import command_argv, _plugin_command_install_hint; "
    )
    if operation == "bootstrap":
        script += "assert command_argv(['chat']) == ['chat']"
    else:
        script += "assert _plugin_command_install_hint('hermes', 'honcho') is None"
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script, str(tmp_path)],
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr
