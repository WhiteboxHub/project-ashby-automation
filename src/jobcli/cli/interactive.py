
"""Interactive TUI for WboxCLI — clean, minimal.

Just a clean prompt. No heavy panels. Information when you need it.
"""

import locale
import os
import sys
import time
from pathlib import Path
try:
    import readline
except ImportError:
    readline = None # type: ignore
import subprocess
import random
import shutil
from datetime import datetime, timedelta

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box
from rich.rule import Rule

console = Console(highlight=False)

# ── Colors ────────────────────────────────────────────────────────────
P = "#c084fc"   # purple (primary)
K = "#f0abfc"   # pink (accent)
D = "#6b7280"   # dim
F = "#4b5563"   # faint
V = "#7c3aed"   # violet


# ── Commands ──────────────────────────────────────────────────────────
COMMANDS = {
    "setup":     ["setup"],
    "apply":     None,
    "continue":  ["continue"],
    "discover":  ["discover"],
    "jobs":      None,
    "status":    None,
    "doctor":    ["doctor"],
    "login":     ["login"],
    "resume":    None,
    "config":    ["config-cmd"],
    "questions": ["questions"],
    "scan":      ["scan"],
    "sync":      ["sync"],
    "log":       None,
    "logs":      None,
    "analytics-backfill": ["analytics-backfill"],
    "backfill":  ["analytics-backfill"],
    "server":    ["server"],
    "dashboard": ["open-dashboard"],
    "reset":     ["reset"],
    "uninstall": ["uninstall"],
    "install": ["install"],
    "update":    ["update"],
    "clear":     None,
    "help":      None,
    "exit":      None,
}

COMMAND_NAMES = list(COMMANDS.keys())


# ── Readline ──────────────────────────────────────────────────────────
class _Completer:
    def __init__(self):
        self.matches = []

    def complete(self, text, state):
        if state == 0:
            self.matches = [c + " " for c in COMMAND_NAMES if c.startswith(text.lower())] if text else []
        return self.matches[state] if state < len(self.matches) else None


def _setup_readline():
    if not readline:
        return None
    history_path = os.path.join(os.path.expanduser("~"), ".jobcli", "history")
    os.makedirs(os.path.dirname(history_path), exist_ok=True)
    try:
        readline.read_history_file(history_path)
    except (FileNotFoundError, PermissionError, OSError):
        pass
    readline.set_history_length(500)
    readline.set_completer(_Completer().complete)
    readline.parse_and_bind("tab: complete")
    if hasattr(readline, "__doc__") and readline.__doc__ and "libedit" in readline.__doc__:
        readline.parse_and_bind("bind ^I rl_complete")
    return history_path


# ── Next-step hints ───────────────────────────────────────────────────
def _next_step_panel(command: str, hint: str = "") -> None:
    """Prominent panel showing the next command to type at the TUI prompt.

    Mirrors ``main.py:_print_next_step`` but uses the bare TUI command name
    (``apply``, ``discover`` …) since the user is already inside the
    interactive shell and never needs to prefix with ``wboxcli``.
    """
    from rich.panel import Panel
    body_lines = [f"  [bold #f0abfc]> {command}[/]"]
    if hint:
        body_lines.append(f"  [{D}]{hint}[/]")
    console.print()
    console.print(
        Panel(
            "\n".join(body_lines),
            title="[bold #d946ef]▶ Next step[/]",
            title_align="left",
            border_style="#c084fc",
            padding=(0, 1),
        )
    )


def _next_hint(text: str) -> None:
    """Lightweight inline breadcrumb between onboarding steps."""
    console.print(f"  [{D}]▶ Next:[/] [{K}]{text}[/]")


# ── Welcome ───────────────────────────────────────────────────────────
def _animate_banner():
    """Interactive welcome: boxed title + block ASCII banner."""
    # The small boxed welcome at the top left
    welcome_box = Panel(
        "[bold #f0abfc]◈ Welcome to W-BOX CLI[/]", 
        box=box.ROUNDED, 
        border_style="#c084fc",
        padding=(0, 1),
        expand=False
    )
    console.print()
    console.print(welcome_box)
    console.print()

    # Huge block ASCII art for "WBOXCLI" on one line
    block_ascii = [
        f"[bold #f0abfc]██╗    ██╗      ██████╗  ██████╗ ██╗  ██╗     ██████╗██╗     ██╗[/]",
        f"[bold #e879f9]██║    ██║      ██╔══██╗██╔═══██╗╚██╗██╔╝    ██╔════╝██║     ██║[/]",
        f"[bold #d946ef]██║ █╗ ██║█████╗██████╔╝██║   ██║ ╚███╔╝     ██║     ██║     ██║[/]",
        f"[bold #c026d3]██║███╗██║╚════╝██╔══██╗██║   ██║ ██╔██╗     ██║     ██║     ██║[/]",
        f"[bold #a78bfa]╚███╔███╔╝      ██████╔╝╚██████╔╝██╔╝ ██╗    ╚██████╗███████╗██║[/]",
        f"[bold #7c3aed] ╚══╝╚══╝       ╚═════╝  ╚═════╝ ╚═╝  ╚═╝     ╚═════╝╚══════╝╚═╝[/]"
    ]
    
    for line in block_ascii:
        console.print(line)
        time.sleep(0.04)

    console.print()
    console.print(f"[{D}]W-BOX CLI is an exclusive autonomous agent. It works ONLY with[/]")
    console.print(f"[{D}]Whitebox Learning (https://whiteboxlearning.com) and will[/]")
    console.print(f"[{D}]not apply on any other external job boards.[/]")
    console.print()


def _validate_llm(provider: str, api_key: str) -> bool:
    """Validate LLM API key with a quick test call."""
    try:
        if provider == "openai":
            import openai
            client = openai.OpenAI(api_key=api_key)
            client.models.list()
            return True
        elif provider == "anthropic":
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            client.messages.create(
                model="claude-3-haiku-20240307",
                max_tokens=1,
                messages=[{"role": "user", "content": "hi"}]
            )
            return True
        elif provider == "gemini":
            from google import genai
            client = genai.Client(api_key=api_key)
            client.models.list()
            return True
        return False
    except Exception:
        return False


def _validate_wbox_and_extension(
    email: str,
    password: str,
    ext_dir: str | None = None,
    *,
    on_progress=None,
) -> tuple[bool, bool, str | None, str]:
    """Validate Whitebox credentials AND load the TalentScreen extension in
    a single visible Chrome launch.

    Returns
    -------
    ``(login_ok, extension_ok, resolved_ext_dir, error_message)``
    """
    from jobcli.utils.extension_helpers import (
        _report as _ext_report,
        get_local_extension_zip,
        maybe_install_local_extension_zip,
        resolve_extension_dir,
        verify_extension_in_browser,
    )

    if get_local_extension_zip():
        _ext_report(on_progress, "Preparing extension from ZIP…")
        maybe_install_local_extension_zip()

    _ext_report(on_progress, "Locating extension files…")
    resolved = resolve_extension_dir(ext_dir)
    if not resolved:
        zip_hint = get_local_extension_zip()
        if zip_hint is None:
            err = (
                "TalentScreen extension not found. Build the extension ZIP, then copy it to "
                "project-talentscreen-wbox-cli/extension/ (any .zip name; see "
                "docs/SETUP_WINDOWS_MAC.md). On Windows: .\\build.ps1 in the "
                "autofill-extension repo, then Copy-Item dist\\*.zip to extension\\."
            )
        else:
            err = (
                "TalentScreen extension not unpacked. ZIP exists but "
                "~/.jobcli/extension_unpacked/ is missing — run: "
                "python -m jobcli.cli.main doctor  (with PYTHONPATH=src set)"
            )
        return (False, False, None, err)

    login_ok, extension_ok, err = verify_extension_in_browser(
        resolved, email, password, progress=on_progress
    )
    return (login_ok, extension_ok, resolved, err)


def _onboarding_complete(config, has_resume: bool) -> bool:
    """True when WBL login, LLM key, and resume are all stored locally."""
    return bool(
        config.job_board_username
        and config.job_board_password
        and (config.openai_api_key or config.anthropic_api_key or config.gemini_api_key)
        and has_resume
    )


def _run_onboarding(force: bool = False):
    """Interactive onboarding selection."""
    from jobcli.cli.main import get_config, get_database
    from jobcli.storage.repositories import ConfigRepository
    import getpass

    db = get_database()
    session = db.get_session()
    try:
        config = get_config()
        has_llm = bool(config.openai_api_key or config.anthropic_api_key or config.gemini_api_key)
        has_wbox = bool(config.job_board_username and config.job_board_password)

        from jobcli.storage.repositories import UserDataRepository

        resume_data = UserDataRepository(session).get_resume()
        has_resume = resume_data is not None

        if not force and _onboarding_complete(config, has_resume):
            console.print(
                f"[green]✓[/green] Welcome back — using saved login "
                f"([cyan]{config.job_board_username}[/cyan]). Type [bold]help[/bold] for commands.\n"
            )
            return

        if force or not has_llm or not has_wbox or not has_resume:
            PURP = "\033[1;38;2;192;132;252m"
            RST = "\033[0m"

            repo = ConfigRepository(session)
            db_config = repo.get_all()

            # ── Step 1 — Whitebox Learning Login + Browser/Extension Test ──
            # A single visible Chrome window doubles as the credential check and the
            # extension smoke test, so we never launch two browsers in a row.
            console.print(f"[{D}]Step 1/3[/] — [bold]Whitebox Learning Credentials[/bold]")
            if force or not db_config.job_board_username or not db_config.job_board_password:
                while True:
                    try:
                        email = input(f"{PURP}Whitebox Email: {RST}").strip()
                    except (KeyboardInterrupt, EOFError):
                        raise KeyboardInterrupt("setup interrupted at email prompt") from None
                    try:
                        password = getpass.getpass(f"{PURP}Whitebox Password: {RST}").strip()
                    except (KeyboardInterrupt, EOFError):
                        raise KeyboardInterrupt("setup interrupted at password prompt") from None

                    def _browser_progress(msg: str) -> None:
                        console.print(f"  [{D}]… {msg}[/]")
                        sys.stdout.flush()

                    console.print()
                    console.print(
                        f"[bold #e879f9]Please wait[/] [{D}]— browser test in progress "
                        f"(usually 15–30 seconds).[/]"
                    )
                    console.print(
                        f"[{D}]  Chrome may open briefly. Do not close it until the checks below finish.[/]"
                    )
                    console.print()

                    login_ok, extension_ok, ext_dir, err = _validate_wbox_and_extension(
                        email,
                        password,
                        db_config.extension_path,
                        on_progress=_browser_progress,
                    )
                    console.print()

                    if login_ok:
                        db_config.job_board_username = email
                        db_config.job_board_password = password
                        if ext_dir:
                            db_config.extension_path = ext_dir
                        console.print(f"[bold white on #d946ef] ✓ [/] [green]Open browser[/green]")
                        if extension_ok:
                            console.print(f"[bold white on #d946ef] ✓ [/] [green]Plugin load (extension loaded)[/green]")
                        else:
                            console.print(f"[yellow]  ⚠ Plugin load could not be confirmed (extension may load on first apply).[/yellow]")
                        console.print(f"[bold white on #d946ef] ✓ [/] [green]Test successful[/green]")
                        break
                    else:
                        if err:
                            console.print(f"[bold white on #c026d3] ✗ [/] [red]Login failed: {err}[/red]")
                        else:
                            console.print(f"[bold white on #c026d3] ✗ [/] [red]Invalid email or password. Please try again.[/red]")
            else:
                console.print(
                    f"  [green]✓[/green] Using saved Whitebox login "
                    f"([cyan]{db_config.job_board_username}[/cyan])"
                )

            # Persist Whitebox + extension path immediately so that even if the
            # user Ctrl+C's during the LLM step we don't lose verified creds.
            repo.save_config(db_config)
            session.commit()
            # Credentials are persisted only in ~/.jobcli/jobcli.db — no ``.env`` is written.
            _next_hint("configure your AI provider so the agent can fill complex forms")

            # ── Step 2 — LLM Provider + API Key ──
            console.print()
            console.print(f"[{D}]Step 2/3[/] — [bold]Select LLM Provider for Automation[/bold]")
            console.print()
            console.print(f"[{K}]> 1. OpenAI (Recommended)[/]")
            console.print(f"  [{D}]Requires OPENAI_API_KEY[/]")
            console.print(f"  2. Anthropic")
            console.print(f"  3. Gemini")
            console.print()

            choice = ""
            while True:
                choice = input(f"{PURP}Select provider (1-3) > {RST}").strip()
                if choice not in ("1", "2", "3"):
                    continue

                provider = "openai" if choice == "1" else "anthropic" if choice == "2" else "gemini"
                prompt_name = "OpenAI" if choice == "1" else "Anthropic" if choice == "2" else "Gemini"

                api_key = input(f"{PURP}Enter {prompt_name} API Key: {RST}").strip()

                console.print()
                console.print(f"[bold #e879f9]Please wait[/] [{D}]— validating {prompt_name} API key…[/]")
                sys.stdout.flush()
                is_valid = _validate_llm(provider, api_key)
                console.print()

                if is_valid:
                    db_config.default_llm_provider = provider
                    if provider == "openai":
                        db_config.openai_api_key = api_key
                    elif provider == "anthropic":
                        db_config.anthropic_api_key = api_key
                    elif provider == "gemini":
                        db_config.gemini_api_key = api_key
                    console.print(f"[bold white on #d946ef] ✓ [/] [green]API key successfully verified[/green]")
                    break
                else:
                    console.print(f"[bold white on #c026d3] ✗ [/] [red]Invalid {prompt_name} API key. Please try again.[/red]")

            repo.save_config(db_config)
            session.commit()
            _next_hint("upload your resume (PDF + JSON) so the agent can answer for you")

            # ── Step 3 — Resume Upload + profile confirmation ──
            console.print()
            console.print(f"[{D}]Step 3/3[/] — [bold]Resume Upload[/bold]")
            if force or not has_resume:
                from jobcli.utils.resume_helpers import (
                    confirm_profile_prompt,
                    load_resume_from_paths,
                    persist_resume,
                    print_profile_summary,
                )

                session.close()

                while True:
                    pdf_path = input(f"{PURP}Path to Resume PDF: {RST}").strip()
                    json_path = input(f"{PURP}Path to Resume JSON: {RST}").strip()

                    try:
                        resume, pdf_resolved, json_resolved, raw_json = load_resume_from_paths(
                            pdf_path, json_path
                        )
                    except ValueError as exc:
                        console.print(f"[bold white on #c026d3] ✗ [/] [red]{exc}[/red]")
                        continue
                    except Exception as exc:
                        console.print(
                            f"[bold white on #c026d3] ✗ [/] [red]Invalid resume JSON: {exc}[/red]"
                        )
                        continue

                    console.print()
                    print_profile_summary(console, resume, pdf_resolved)

                    if not confirm_profile_prompt():
                        console.print(f"[{D}]Re-enter your resume paths to try again.[/]\n")
                        continue

                    persist_resume(
                        resume, pdf_resolved, json_resolved, raw_json_resume=raw_json
                    )
                    console.print("[bold green]✓ Resume uploaded successfully[/bold green]\n")

                    _next_step_panel(
                        "discover",
                        "pull fresh job listings from Whitebox",
                    )
                    _exec(["discover"])
                    _next_step_panel(
                        "apply",
                        "start applying when you are ready (run manually)",
                    )
                    break
            else:
                session.close()

            console.print(f"\n[{K}]✓ Setup complete! You are ready to discover and apply to jobs.[/]")
    except KeyboardInterrupt:
        console.print(
            "\n[yellow]Setup interrupted.[/yellow] Progress is saved after each completed step."
        )
        console.print(
            "[dim]Run [cyan]wboxcli[/cyan] again to continue, or [cyan]wboxcli login[/cyan] "
            "to save credentials only.[/dim]\n"
        )
    except Exception as e:
        console.print(f"[red]Error during setup: {e}[/red]")
        console.print_exception()
    finally:
        try:
            session.close()
        except Exception:
            pass


def _print_welcome():
    """Interactive welcome and onboarding."""
    _animate_banner()
    _run_onboarding()

    # Greeting
    hour = datetime.now().hour
    name = ""

    try:
        from jobcli.cli.main import get_database
        from jobcli.storage.repositories import UserDataRepository
        db = get_database()
        session = db.get_session()
        resume = UserDataRepository(session).get_resume()
        session.close()
        if resume and resume.personal.first_name:
            name = resume.personal.first_name
    except Exception:
        pass

    if hour < 12:
        greeting = "Good morning"
    elif hour < 17:
        greeting = "Good afternoon"
    else:
        greeting = "Good evening"

    greeting_str = f"{greeting}, {name}" if name else greeting

    pending_eligible = 0
    state_known = False
    try:
        from jobcli.cli.main import get_database
        from jobcli.storage.repositories import JobRepository

        db = get_database()
        session = db.get_session()
        repo = JobRepository(session)
        pending_jobs = repo.list_pending()
        pending_eligible = len(pending_jobs)
        session.close()
        state_known = True

        console.print(f"  {greeting_str}.")
        console.print()
        console.print(f"  [{D}]You have [bold]{pending_eligible}[/bold] pending CLI-friendly job(s) in the local queue.[/]")
    except Exception:
        console.print(f"  {greeting_str}.")

    # Prominent next-step panel — picks the single most useful command
    # based on local state so the user never has to guess what to type.
    if state_known and pending_eligible > 0:
        _next_step_panel(
            "apply",
            f"start applying to your {pending_eligible} pending job(s) "
            "(Chrome opens visibly; type `q` or Ctrl+C to stop)",
        )
    else:
        _next_step_panel(
            "discover",
            "pull fresh job listings from Whitebox, then run `apply`",
        )

    console.print()
    console.print(f"  [{D}]Type a command to get started, or[/] [{K}]help[/] [{D}]to see options.[/]")
    console.print(f"  [{D}]Use[/] [{K}]Tab[/] [{D}]to autocomplete,[/] [{K}]↑↓[/] [{D}]for history.[/]")
    console.print()


# ── Helpers ───────────────────────────────────────────────────────────
def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent.parent


def _running_from_dev_tree() -> bool:
    """True when launched from build.bat / repo (PYTHONPATH=src or project .venv)."""
    py_path = os.environ.get("PYTHONPATH", "").replace("\\", "/")
    if "src" in py_path.split(os.pathsep) or py_path.rstrip("/").endswith("/src"):
        return True
    if os.name == "nt":
        venv_py = _project_root() / ".venv" / "Scripts" / "python.exe"
        if venv_py.is_file():
            try:
                return Path(sys.executable).resolve() == venv_py.resolve()
            except OSError:
                pass
    return False


def _find_wboxcli_bin() -> str:
    venv_dir = os.path.join(os.path.expanduser("~"), ".jobcli", "venv")
    candidate = os.path.join(venv_dir, "Scripts" if os.name == "nt" else "bin", "wboxcli.exe" if os.name == "nt" else "wboxcli")
    if os.path.exists(candidate):
        return candidate
    return shutil.which("wboxcli") or "wboxcli"


def _subprocess_encoding() -> str:
    """Encoding for child CLI stdout (Windows consoles often use cp1252, not UTF-8)."""
    return (
        getattr(sys.stdout, "encoding", None)
        or locale.getpreferredencoding(False)
        or "utf-8"
    )


def _wboxcli_command(args: list[str]) -> list[str]:
    """Build argv for a wboxcli subcommand (same Python as the TUI when in dev tree)."""
    # build.bat sets PYTHONPATH=src — use that interpreter so Playwright/browsers match.
    if _running_from_dev_tree():
        return [sys.executable, "-m", "jobcli.cli.main"] + args
    wboxcli = _find_wboxcli_bin()
    if wboxcli != "wboxcli" and os.path.isfile(wboxcli):
        return [wboxcli] + args
    return [sys.executable, "-m", "jobcli.cli.main"] + args


def _exec(args: list[str]):
    """Run a wboxcli subcommand, streaming output."""
    cmd = _wboxcli_command(args)

    console.print(f"\n  [{D}]$ wboxcli {' '.join(args)}[/]")
    console.print()

    env = os.environ.copy()
    if _running_from_dev_tree():
        src = str(_project_root() / "src")
        existing = env.get("PYTHONPATH", "")
        if src not in existing.replace("\\", "/").split(os.pathsep):
            env["PYTHONPATH"] = src if not existing else f"{src}{os.pathsep}{existing}"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if os.name == "nt":
        env.setdefault("PYTHONUTF8", "1")

    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=os.getcwd(),
            text=True,
            encoding=_subprocess_encoding(),
            errors="replace",
            env=env,
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in iter(proc.stdout.readline, ""):
            sys.stdout.write(line)
            sys.stdout.flush()
        proc.wait()
        console.print()
    except KeyboardInterrupt:
        if proc is not None:
            try:
                # Give child process 30s to gracefully shutdown.
                # The child process itself also listens for Ctrl+C and handles
                # double-Ctrl+C force quit logic.
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                # If it gets stuck for 30 seconds, force kill.
                proc.terminate()
                proc.wait()
            except KeyboardInterrupt:
                # If user spams Ctrl+C during wait, force kill.
                proc.terminate()
                proc.wait()
        console.print(f"\n  [{D}]cancelled[/]\n")
    except FileNotFoundError:
        try:
            subprocess.run(
                _wboxcli_command(args),
                cwd=os.getcwd(),
                env=env,
            )
        except KeyboardInterrupt:
            console.print(f"\n  [{D}]cancelled[/]\n")


# ── Internal Commands ─────────────────────────────────────────────────
def _cmd_help():
    console.print()
    console.print(f"  [{K}]Commands:[/]")
    console.print()

    groups = [
        ("Getting started", [
            ("setup",     "One-shot validation + extension download + browser test"),
            ("login",     "Configure credentials"),
            ("resume",    "Upload resume PDF + JSON"),
            ("config",    "View/edit configuration"),
            ("questions", "Pre-fill common answers"),
        ]),
        ("Running", [
            ("apply",          "Apply to all pending jobs (after discover)"),
            ("jobs --ats ashby", "List pending Ashby jobs with selectable IDs"),
            ("apply --ats ashby", "Apply all pending Ashby jobs, one at a time"),
            ("apply --ats ashby -j 12 -j 19", "Apply selected Ashby job IDs"),
            ("apply --url URL","Apply to a specific URL"),
            ("continue",       "Resume last apply stopped with Ctrl+C"),
            ("discover",       "Discover jobs from WBL API (cli_window, paginated)"),
            ("scan",           "Scan ATS portals for openings"),
        ]),
        ("Info", [
            ("status",    "Show current status"),
            ("jobs",      "List pending jobs"),
            ("log",       "Latest apply-run jobs (table); log --job-id N for raw tail"),
            ("logs",      "Alias for log"),
            ("doctor",    "Health check"),
            ("sync",      "Sync learned patterns with server"),
            ("analytics-backfill --since-hours N", "Upload apply analytics, last 24h by default"),
            ("backfill",  "Alias for analytics-backfill"),
        ]),
        ("Other", [
            ("server",    "Start web UI dashboard"),
            ("dashboard", "Open Whitebox dashboard in browser"),
            ("install",   "Install WboxCLI (one-line installer)"),
            ("reset",     "Clear login, API keys, resume (keep jobs)"),
            ("db reset",  "Wipe entire database (via wboxcli db reset)"),
            ("update",    "Update WboxCLI code and dependencies"),
            ("uninstall", "Full uninstallation"),
            ("clear",     "Clear the screen"),
            ("exit",      "Exit"),
        ]),
    ]

    for group_name, cmds in groups:
        console.print(f"  [{D}]{group_name}[/]")
        for cmd_name, desc in cmds:
            console.print(f"    [{K}]{cmd_name:<20}[/] [{D}]{desc}[/]")
        console.print()


def _cmd_status():
    try:
        from jobcli.cli.main import get_config, get_database
        from jobcli.storage.repositories import JobRepository, UserDataRepository

        config = get_config()
        db = get_database()
        session = db.get_session()
        has_llm = bool(config.openai_api_key or config.anthropic_api_key or config.gemini_api_key)
        has_wbox = bool(config.job_board_username and config.job_board_password)
        resume = UserDataRepository(session).get_resume()
        pending = len(JobRepository(session).list_pending())
        session.close()

        console.print()
        items = [
            ("LLM",       f"[green]✓[/] {config.default_llm_provider}" if has_llm else "[red]✗[/] not configured"),
            ("Login",     f"[green]✓[/] {config.job_board_username}" if has_wbox else "[yellow]⚠[/] not set"),
            ("Resume",    f"[green]✓[/] {resume.personal.first_name} {resume.personal.last_name}" if resume else "[red]✗[/] not uploaded"),
            ("Jobs",      f"[{K}]{pending}[/] pending"),
            ("Browser",   f"visible" if not config.headless else "headless"),
        ]
        for label, val in items:
            console.print(f"  [{D}]{label:<12}[/] {val}")
        console.print()

    except Exception as e:
        console.print(f"  [red]error: {e}[/]\n")


def _cmd_jobs(ats: str | None = None):
    try:
        from jobcli.cli.main import get_database
        from jobcli.storage.repositories import JobRepository

        with console.status(f"[{D}]loading...", spinner="dots", spinner_style=P):
            db = __import__("jobcli.cli.main", fromlist=["get_database"]).get_database()
            session = db.get_session()
            pending = JobRepository(session).list_pending()
            session.close()

        if ats:
            if ats.strip().lower() != "ashby":
                console.print("\n  [red]Only the Ashby filter is currently supported: jobs --ats ashby[/]\n")
                return
            pending = [
                job for job in pending
                if getattr(job.ats_type, "value", str(job.ats_type)).lower() == "ashby"
                or "ashbyhq.com" in job.url.lower()
            ]

        if not pending:
            console.print(f"\n  [{D}]No pending jobs. Run[/] [{K}]discover[/] [{D}]to find some.[/]\n")
            return

        console.print()
        for i, job in enumerate(pending, 1):
            title = job.title or "untitled"
            url = job.url[:60] + "…" if len(job.url) > 60 else job.url
            console.print(f"  [{D}]{i:>3}.[/] [cyan]ID {job.id}[/]  [{K}]{title}[/]")
            console.print(f"       [{F}]{url}[/]")
        label = " Ashby" if ats else ""
        console.print(f"\n  [{D}]{len(pending)}{label} jobs pending[/]")
        if ats and pending:
            console.print(f"  [{D}]Apply all:[/] [{K}]apply --ats ashby[/]")
            console.print(f"  [{D}]Apply selected:[/] [{K}]apply --ats ashby -j ID -j ID[/]\n")
        else:
            console.print()

    except Exception as e:
        console.print(f"  [red]error: {e}[/]\n")


# ── Dispatch ──────────────────────────────────────────────────────────
def _dispatch(raw: str):
    parts = raw.strip().split()
    if not parts:
        return

    # Accept pasted README-style commands: `wboxcli apply`, `wboxcli discover`, …
    if parts[0].lower() in ("jobcli", "wboxcli"):
        parts = parts[1:]
        if not parts:
            console.print(f"\n  [{D}]type a subcommand after[/] [{K}]wboxcli[/] [{D}](e.g. apply, discover)[/]\n")
            return

    cmd = parts[0].lower()
    args = parts[1:]

    # Internal
    if cmd == "help":
        _cmd_help()
        return
    if cmd == "status":
        _cmd_status()
        return
    if cmd == "jobs":
        if args and args[0].lower() == "apply":
            _exec(["apply"] + args[1:])
            return
        ats = None
        if len(args) == 2 and args[0].lower() == "--ats":
            ats = args[1]
        elif args:
            console.print(f"\n  [{D}]usage:[/] [{K}]jobs [--ats ashby][/ ]\n")
            return
        _cmd_jobs(ats=ats)
        return
    if cmd in ("log", "logs"):
        from jobcli.cli.main import _run_log

        job_id: int | None = None
        tail = 60
        show_urls = False
        i = 0
        while i < len(args):
            a = args[i].lower()
            if a in ("--job-id", "-j") and i + 1 < len(args):
                job_id = int(args[i + 1])
                i += 2
                continue
            if a in ("--tail", "-n") and i + 1 < len(args):
                tail = int(args[i + 1])
                i += 2
                continue
            if a == "--urls":
                show_urls = True
                i += 1
                continue
            if args[i].isdigit() and job_id is None:
                job_id = int(args[i])
                i += 1
                continue
            i += 1
        _run_log(job_id=job_id, tail=tail, show_urls=show_urls)
        return
    if cmd in ("clear", "cls"):
        os.system("cls" if os.name == "nt" else "clear")
        _print_welcome()
        return
    if cmd in ("exit", "quit", "q"):
        return

    # Apply
    if cmd == "apply":
        _exec(["apply"] + args)
        return
    if cmd == "continue":
        _exec(["continue"])
        return

    # Resume aliases — accept the short form ``resume`` *and* the full
    # underlying CLI command ``resume-upload`` so users who copy a command
    # from the README into the TUI don't get an "unknown command" wall.
    if cmd in ("resume", "resume-upload"):
        _exec(["resume-upload"] + args)
        return

    # Login / Setup mapping
    if cmd in ("login", "setup"):
        _run_onboarding(force=True)
        return

    # Questions must run in-process — _exec pipes stdout so Rich Prompt.ask never
    # reaches the terminal (header prints, then prompts hang invisible).
    if cmd == "questions":
        from jobcli.cli.main import questions as questions_cmd

        console.print()
        try:
            questions_cmd()
        except KeyboardInterrupt:
            console.print(f"\n  [{D}]cancelled[/]\n")
        except Exception as e:
            console.print(f"[red]Error during questions: {e}[/red]")
        return

    if cmd == "reset":
        from jobcli.cli.main import _run_reset

        try:
            _run_reset(force=False)
            _run_onboarding(force=True)
        except Exception as e:
            console.print(f"[red]Error during reset: {e}[/red]")
        return

    # Uninstall mapping (deletes all CLI data and shims, then exits cleanly)
    if cmd == "uninstall":
        from jobcli.cli.main import uninstall
        try:
            uninstall(force=False)
            sys.exit(0)
        except SystemExit:
            sys.exit(0)
        except Exception as e:
            console.print(f"[red]Error during uninstall: {e}[/red]")
        return

    # Update mapping (runs update script and exits cleanly so new code is loaded)
    if cmd == "update":
        import subprocess
        console.print(f"\n  [{D}]Running WboxCLI Update...[/]")
        try:
            script_path = _project_root() / "scripts" / "wboxcli.sh"
            powershell_path = _project_root() / "scripts" / "install.ps1"
            if os.name != "nt":
                subprocess.run(["bash", str(script_path), "update"], check=True)
            else:
                subprocess.run(["powershell.exe", "-File", str(powershell_path)], check=True)
            console.print(f"\n  [green]Update complete! Please restart WboxCLI to run the newly updated version.[/green]\n")
            sys.exit(0)
        except SystemExit:
            sys.exit(0)
        except Exception as e:
            console.print(f"[red]Error during update: {e}[/red]")
        return

    # Install mapping (runs one-line installer for the current branch)
    if cmd == "install":
        import subprocess
        console.print(f"\n  [{D}]Running WboxCLI Installer...[/]")
        try:
            script_sh = _project_root() / "scripts" / "install.sh"
            script_ps1 = _project_root() / "scripts" / "install.ps1"
            if os.name != "nt":
                subprocess.run(["bash", str(script_sh)], check=True)
            else:
                subprocess.run(["powershell.exe", "-File", str(script_ps1)], check=True)
            console.print(f"\n  [green]Install complete! Follow any on-screen instructions.[/green]\n")
            sys.exit(0)
        except SystemExit:
            sys.exit(0)
        except Exception as e:
            console.print(f"[red]Error during install: {e}[/red]")
        return

    # Standard
    if cmd in COMMANDS and COMMANDS[cmd] is not None:
        _exec(COMMANDS[cmd] + args)
        return

    # Unknown
    close = [c for c in COMMAND_NAMES if c.startswith(cmd[:2]) and c != cmd]
    if close:
        suggestions = ", ".join(f"[{K}]{c}[/]" for c in close[:3])
        console.print(f"\n  [{D}]unknown command:[/] {cmd}  [{D}]— did you mean {suggestions}?[/]\n")
    else:
        console.print(f"\n  [{D}]unknown command:[/] {cmd}  [{D}]— type[/] [{K}]help[/] [{D}]for options[/]\n")


# ── Prompt ────────────────────────────────────────────────────────────
def _prompt() -> str:
    # ANSI codes (Rich can't style input/readline)
    PURP = "\033[1;38;2;192;132;252m"
    RST = "\033[0m"

    try:
        return input(f"  {PURP}>{RST} ").strip()
    except EOFError:
        return "exit"


# ── Session ───────────────────────────────────────────────────────────
def interactive_session():
    os.system("cls" if os.name == "nt" else "clear")
    history_path = _setup_readline()
    _print_welcome()

    try:
        while True:
            try:
                user_input = _prompt()
                if not user_input:
                    continue
                if user_input.lower() in ("exit", "quit", "q"):
                    console.print(f"\n  [{D}]goodbye, Have a great day!😀 [/]\n")
                    break
                _dispatch(user_input)
            except KeyboardInterrupt:
                console.print(f"  [{D}]^C[/]")

    finally:
        if readline and history_path:
            try:
                readline.write_history_file(history_path)
            except (PermissionError, OSError):
                pass
