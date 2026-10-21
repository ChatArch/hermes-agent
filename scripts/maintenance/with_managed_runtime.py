"""Execute a task script in the installed Hermes managed runtime."""
from __future__ import annotations

import ast
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launcher", type=Path, required=True)
    parser.add_argument("script", type=Path)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    script = args.script.resolve()
    assert script.parent == Path(__file__).resolve().parent
    launcher = args.launcher.resolve()
    env = dict(os.environ, HERMES_DISABLE_LAZY_INSTALLS="1")
    command = json.loads(subprocess.check_output(
        [str(launcher), "--print-runtime-command"], text=True, encoding="utf-8", errors="replace", timeout=30, env=env))
    assert command[1:3] == ["-I", "-c"] and len(command) == 4
    tree = ast.parse(command[3])
    last = tree.body[-1]
    assert isinstance(last, ast.Expr) and isinstance(last.value, ast.Call)
    assert ast.unparse(last.value.func) == "runpy.run_module"
    assert last.value.args[0].value == "hermes_cli.main"
    tree.body[-1:] = ast.parse(
        f"sys.argv = {[str(script), *args.arguments]!r}\n"
        f"runpy.run_path({str(script)!r}, run_name='__main__')").body
    command[3] = ast.unparse(ast.fix_missing_locations(tree))
    os.execvpe(command[0], command, env)


if __name__ == "__main__":
    raise SystemExit(main())
