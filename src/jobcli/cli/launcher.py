"""Ensure ``wboxcli`` resolves to the managed ``~/.jobcli/venv`` install."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def jobcli_venv_root() -> Path:
    return Path.home() / ".jobcli" / "venv"


def jobcli_venv_python() -> Path | None:
    root = jobcli_venv_root()
    if os.name == "nt":
        candidate = root / "Scripts" / "python.exe"
    else:
        candidate = root / "bin" / "python"
    return candidate if candidate.is_file() else None


def managed_wboxcli_shim() -> Path | None:
    local_bin = Path.home() / ".local" / "bin"
    if os.name == "nt":
        candidate = local_bin / "wboxcli.cmd"
    else:
        candidate = local_bin / "wboxcli"
    return candidate if candidate.is_file() else None


def is_running_from_managed_venv() -> bool:
    venv_py = jobcli_venv_python()
    if venv_py is None:
        return False
    try:
        return Path(sys.executable).resolve() == venv_py.resolve()
    except OSError:
        return False


def reexec_via_managed_venv(argv: list[str] | None = None) -> None:
    """Replace this process with ``python -m jobcli.cli.entry`` from ~/.jobcli/venv."""
    venv_py = jobcli_venv_python()
    if venv_py is None:
        _print_bootstrap_error(
            "WboxCLI is not installed. Run the installer:\n"
            "  Windows: irm https://raw.githubusercontent.com/WhiteboxHub/"
            "project-ashby-automation/main/scripts/install.ps1 | iex\n"
            "  macOS:   curl -fsSL https://raw.githubusercontent.com/WhiteboxHub/"
            "project-ashby-automation/main/scripts/install.sh | bash"
        )
        raise SystemExit(1)

    cmd = [str(venv_py), "-m", "jobcli.cli.entry", *(argv if argv is not None else sys.argv[1:])]
    os.execv(str(venv_py), cmd)


def ensure_managed_launcher() -> None:
    """If a broken global shim started us without ``jobcli``, re-exec from venv."""
    if is_running_from_managed_venv():
        return
    try:
        import jobcli  # noqa: F401
    except ModuleNotFoundError:
        reexec_via_managed_venv()


def warn_if_misconfigured_launcher() -> None:
    """Print a hint when PATH may point at the wrong ``wboxcli``."""
    if is_running_from_managed_venv():
        return
    if jobcli_venv_python() is None:
        return

    which = shutil.which("wboxcli")
    expected = managed_wboxcli_shim()
    lines = [
        "[dim]Tip: restart the terminal after install so `wboxcli` uses ~/.local/bin.[/dim]",
    ]
    if which and expected:
        try:
            if Path(which).resolve() != expected.resolve():
                lines.insert(
                    0,
                    f"[yellow]Note:[/yellow] `wboxcli` on PATH is [dim]{which}[/dim]; "
                    f"expected [cyan]{expected}[/cyan].",
                )
        except OSError:
            pass
    try:
        from rich.console import Console

        Console(highlight=False).print("\n".join(lines))
    except Exception:
        print("\n".join(lines), file=sys.stderr)


def _print_bootstrap_error(message: str) -> None:
    try:
        from rich.console import Console

        Console(highlight=False).print(f"[red]{message}[/red]")
    except Exception:
        print(message, file=sys.stderr)


def remove_stale_global_shims() -> list[str]:
    """Remove broken global ``wboxcli`` shims outside ``~/.jobcli/venv``."""
    removed: list[str] = []
    venv_marker = str(jobcli_venv_root().resolve()).lower()
    managed = managed_wboxcli_shim()
    candidates: list[Path] = []

    if os.name == "nt":
        py_root = Path.home() / "AppData" / "Local" / "Programs" / "Python"
        if py_root.is_dir():
            for exe in py_root.rglob("wboxcli.exe"):
                candidates.append(exe)
            for script in py_root.rglob("wboxcli-script.py"):
                candidates.append(script)
    else:
        for name in ("wboxcli", "wboxcli.exe"):
            path = shutil.which(name)
            if path:
                candidates.append(Path(path))

    for path in candidates:
        try:
            resolved = str(path.resolve()).lower()
        except OSError:
            continue
        if venv_marker in resolved:
            continue
        if managed:
            try:
                if path.resolve() == managed.resolve():
                    continue
            except OSError:
                pass
        try:
            path.unlink()
            removed.append(str(path))
        except OSError:
            pass
    return removed
