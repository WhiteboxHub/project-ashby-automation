"""Main CLI application using Typer."""

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

# Fix UnicodeEncodeError on Windows terminals when printing checkmarks/symbols
if hasattr(sys.stdout, 'reconfigure') and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

# NOTE: TLS trust roots are configured in ``jobcli/__init__.py`` so that all
# entry points (CLI, tests, interactive) inherit OS-native CA support before
# any HTTP client is built. See ``jobcli/utils/tls.py``.

import typer
from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

from jobcli.orchestration.engine import ApplicationEngine
from jobcli.profile.schemas import ApplicationStatus, CommonQuestions, Config, InteractionMode, Job, ResumeData
from jobcli.storage.models import Database
from jobcli.orchestration.wbox_discoverer import WboxDiscoverer
from jobcli.storage.repositories import (
    ConfigRepository,
    JobRepository,
    UserDataRepository,
)
from jobcli.utils.browser_closed import BrowserClosed
from jobcli.utils.constants import DASHBOARD_SUMMARY_DAYS, REFERENCE_LINKS_COUNT, SUPPORTED_DOMAINS
from jobcli.analytics.service import (
    build_apply_run_log,
    flush_usage_events,
    resolve_user_id,
    track_usage_event,
)
from jobcli.analytics.tracker import SessionTracker

app = typer.Typer(
    name="wboxcli",
    help="Production-grade CLI for automated job applications",
)

db_app = typer.Typer(help="Local database maintenance")
app.add_typer(db_app, name="db")

@db_app.command("send-daily-report")
def db_send_daily_report(
    mock: bool = typer.Option(False, "--mock", help="Send mock report for testing"),
) -> None:
    """Fetch today's ATS stats from the backend and email them using CLI SMTP credentials."""
    import os
    from jobcli.analytics.report_mailer import send_daily_report
    server_url = os.getenv("API_URL", "https://whitebox-learning.com/api")
    send_daily_report(server_url, mock=mock)

console = Console()

# Default config directory
CONFIG_DIR = Path.home() / ".jobcli"
CONFIG_FILE = CONFIG_DIR / "config.json"
DATABASE_FILE = CONFIG_DIR / "jobcli.db"


def _print_next_step(command: str, hint: str = "") -> None:
    """Print a prominent boxed "Next" hint after every command succeeds.

    Keeps the flow visible to the user at every step:
        login → resume-upload → setup → discover → apply

    The hint is rendered as a Rich panel so it sticks out from regular log
    output and the user can't miss it as the terminal scrolls.
    """
    from rich.panel import Panel
    body_lines = [f"  [bold cyan]wboxcli {command}[/bold cyan]"]
    if hint:
        body_lines.append(f"  [dim]{hint}[/dim]")
    console.print()
    console.print(
        Panel(
            "\n".join(body_lines),
            title="[bold green]▶ Next step[/bold green]",
            title_align="left",
            border_style="green",
            padding=(0, 1),
        )
    )


def _suggest_after_login_or_resume() -> tuple[str, str]:
    """Pick the right next command based on what's still missing.

    Walks the canonical flow login → resume-upload → setup → discover → apply
    and returns the first step the user hasn't completed yet.
    """
    cfg = get_config()
    if not (cfg.resume_pdf_path and cfg.resume_json_path):
        return "resume-upload --pdf <file.pdf> --json <file.json>", "load your resume"
    ext = (cfg.extension_path or "").strip()
    if not ext or not Path(ext).exists():
        return "setup", "download the TalentScreen extension and verify your setup"
    return "discover", "pull job listings from WBL"

# ``wboxcli config --key`` aliases for stored fields.
CONFIG_CMD_KEY_ALIASES: dict[str, str] = {}


def ensure_config_dir() -> None:
    """Ensure config directory exists."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def get_database() -> Database:
    """Get database instance."""
    db_path_env = os.getenv("DATABASE_PATH")
    if db_path_env:
        db_path = Path(os.path.expandvars(os.path.expanduser(db_path_env)))
    else:
        ensure_config_dir()
        db_path = DATABASE_FILE

    db = Database(f"sqlite:///{db_path.as_posix()}")
    db.create_tables()
    return db


def resolve_active_sqlite_database_path() -> Path:
    """Filesystem path to the active SQLite DB (same rules as ``get_database``)."""
    raw = os.getenv("DATABASE_PATH")
    if raw:
        return Path(os.path.expandvars(os.path.expanduser(raw))).resolve()
    ensure_config_dir()
    return DATABASE_FILE.resolve()


def get_config() -> Config:
    """Load configuration from the local SQLite ``config`` table.

    There is **no ``.env`` loading** — all settings (credentials, LLM keys,
    resume paths, API base URL, extension path) are written by interactive
    commands (``login``, ``resume-upload``, ``setup``) and persisted in
    ``~/.jobcli/jobcli.db``. To change a value later, use
    ``wboxcli config --key <name> --set <value>`` or re-run ``wboxcli login``.
    """
    db = get_database()
    session = db.get_session()
    config_repo = ConfigRepository(session)

    try:
        config = config_repo.get_all()
    except Exception:
        config = Config()

    # Browser must always be visible while applying — no env switch.
    config.headless = False

    session.close()
    return config


def save_config(config: Config) -> None:
    """Save configuration."""
    db = get_database()
    session = db.get_session()
    config_repo = ConfigRepository(session)
    config_repo.save_config(config)
    session.close()


def _track_command_event(
    *,
    db: Database,
    config: Config,
    command: str,
    started_at: float,
    result: str,
    jobs_attempted_count: Optional[int] = None,
    jobs_submitted_count: Optional[int] = None,
    jobs_failed_count: Optional[int] = None,
    metadata: Optional[dict] = None,
    processed_job_ids: Optional[list[int]] = None,
    exit_reason: Optional[str] = None,
) -> Optional[dict]:
    if not config.tracking_enabled:
        return None
    try:
        if command == "apply":
            from jobcli.analytics.service import track_apply_analytics

            return track_apply_analytics(
                db,
                config,
                result=result,
                run_started_at=started_at,
                jobs_attempted_count=int(jobs_attempted_count or 0),
                jobs_submitted_count=int(jobs_submitted_count or 0),
                jobs_failed_count=int(jobs_failed_count or 0),
                processed_job_ids=processed_job_ids or [],
                exit_reason=exit_reason,
                extra_metadata=metadata,
            )
        duration_ms = int((time.time() - started_at) * 1000)
        user_id = resolve_user_id(config.job_board_username)
        track_usage_event(
            db,
            user_id=user_id,
            event_name="cli_command_completed",
            command=command,
            result=result,
            duration_ms=duration_ms,
            jobs_attempted_count=jobs_attempted_count,
            jobs_submitted_count=jobs_submitted_count,
            jobs_failed_count=jobs_failed_count,
            metadata=metadata or {},
        )
        return flush_usage_events(db, batch_size=50)
    except Exception:
        return {"status": "error", "count": 0}


def _require_wbl_credentials_for_discovery(config: Config) -> None:
    """Exit with a clear message unless WBL username/password are available (DB or env)."""
    u = (config.job_board_username or os.getenv("JOBCLI_USERNAME") or "").strip()
    p = config.job_board_password or os.getenv("JOBCLI_PASSWORD")
    if not u or not p:
        console.print(
            "[red]Missing WBL credentials or API URL. Run [cyan]setup[/cyan] or [cyan]login[/cyan] first.[/red]"
        )
        raise typer.Exit(1)


def ensure_configured(config: Config, require_job_board: bool = True) -> None:
    """Ensure the user has logged in and configured required credentials."""
    if require_job_board and (not config.job_board_username or not config.job_board_password):
        console.print("[yellow]Warning: Missing job board credentials.[/yellow]")
        console.print("Run [cyan]login[/cyan] if you need to discover jobs or view Whitebox-hosted listings.")
        
    has_llm = config.openai_api_key or config.anthropic_api_key or config.gemini_api_key
    if not has_llm:
        console.print("\n[yellow]Warning: No LLM API keys configured. Phase 2 (Autonomous Reasoning) will be disabled.[/yellow]")
        console.print("Run [cyan]login[/cyan] to unlock full AI automation capabilities.\n")


def print_dashboard_summary(session):
    """Print the 7-day dashboard summary as requested by the user."""
    from rich.panel import Panel
    from rich.table import Table
    from rich import box
    
    repo = JobRepository(session)
    stats = repo.get_dashboard_stats()
    
    console.print()
    summary_text = (
        f"[bold white]Total Jobs present in WBL for last {DASHBOARD_SUMMARY_DAYS} days:[/] [bold cyan]{stats['total_wbl']}[/]\n"
        f"[bold white]Already applied for you -[/] [bold green]{stats['applied_count']}[/]\n"
        f"[bold white]Remaining total jobs -[/] [bold #f0abfc]{stats['remaining_count']}[/]\n"
        f"[bold white]CLI friendly -[/] [bold #c084fc]{stats['cli_friendly']}[/]\n"
        f"[bold white]Unsupported / skipped -[/] [bold yellow]{stats.get('unsupported_skipped', 0)}[/]"
    )
    
    console.print(Panel(summary_text, title="[bold #c084fc]WBox Dashboard Summary[/]", border_style="#c084fc", expand=False))
    
    if stats['latest_links']:
        console.print(f"\n[bold #f0abfc]Newest-first {len(stats['latest_links'])} job links for reference:[/]")
        table = Table(box=box.SIMPLE, show_header=True, header_style="bold #c084fc")
        table.add_column("Title", style="cyan")
        table.add_column("URL", style="blue")
        table.add_column("Date", style="dim")
        
        for job in stats['latest_links']:
            url = job['url'][:50] + "..." if len(job['url']) > 50 else job['url']
            ca = job['created_at']
            date_s = ca.strftime("%Y-%m-%d") if hasattr(ca, "strftime") else str(ca)
            table.add_row(job['title'] or "Untitled", url, date_s)
        
        console.print(table)


def _print_apply_run_summary_table(
    processed_job_ids: list[int],
    *,
    track_result: Optional[str] = None,
) -> None:
    """Show Rich table of jobs from the run just finished (or last jsonl run)."""
    from jobcli.utils.apply_run_report import (
        print_apply_run_table_for_job_ids,
        print_last_apply_run_table,
    )

    if not processed_job_ids:
        print_last_apply_run_table(console)
        return
    db = get_database()
    with db.get_session() as session:
        print_apply_run_table_for_job_ids(
            session,
            processed_job_ids,
            console=console,
            result=track_result,
        )


def _run_log(
    job_id: Optional[int] = None,
    tail: int = 60,
    show_urls: bool = False,
) -> None:
    """Default: latest apply-run job table; with --job-id: per-job application log tail."""
    if job_id is not None:
        from jobcli.utils.log_viewer import collect_log_lines

        db = get_database()
        with db.get_session() as session:
            lines = collect_log_lines(session, job_id=job_id, tail=tail)
        for line in lines:
            console.print(line)
        if not lines:
            console.print(f"[yellow]No logs for job_id={job_id}[/yellow]")
        return

    from jobcli.utils.apply_run_report import print_last_apply_run_table

    print_last_apply_run_table(console, show_urls=show_urls)


def _dispose_active_database() -> None:
    """Close SQLite engine handles without recreating tables (used before uninstall)."""
    import gc

    from sqlalchemy import create_engine

    try:
        db_path = resolve_active_sqlite_database_path()
        if db_path.is_file():
            eng = create_engine(f"sqlite:///{db_path.as_posix()}", echo=False)
            eng.dispose()
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{db_path}{suffix}")
                if sidecar.is_file():
                    try:
                        sidecar.unlink()
                    except OSError:
                        pass
    except Exception:
        pass
    gc.collect()


def _rmtree_robust(path: Path) -> None:
    """Remove a directory tree; clear read-only bits on Windows before retry."""
    import shutil
    import stat

    def _on_rm_error(func, p, _exc_info):  # type: ignore[no-untyped-def]
        os.chmod(p, stat.S_IWRITE)
        func(p)

    shutil.rmtree(path, onerror=_on_rm_error)


def _schedule_post_exit_jobcli_removal(config_dir: Path, shim_path: Path) -> None:
    """Remove install dir after this process exits (venv cannot delete itself while running)."""
    import shlex
    import subprocess

    config_s = str(config_dir.resolve())
    shim_s = str(shim_path.resolve())

    if os.name == "nt":
        # Escape single quotes for PowerShell single-quoted literals.
        config_ps = config_s.replace("'", "''")
        shim_ps = shim_s.replace("'", "''")
        ps = (
            "Start-Sleep -Seconds 2; "
            f"if (Test-Path -LiteralPath '{config_ps}') "
            f"{{ Remove-Item -LiteralPath '{config_ps}' -Recurse -Force -ErrorAction SilentlyContinue }}; "
            f"if (Test-Path -LiteralPath '{shim_ps}') "
            f"{{ Remove-Item -LiteralPath '{shim_ps}' -Force -ErrorAction SilentlyContinue }}"
        )
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
            close_fds=True,
            creationflags=creationflags,
        )
        return

    # macOS / Linux — detached shell removes tree after the CLI process exits.
    config_q = shlex.quote(config_s)
    shim_q = shlex.quote(shim_s)
    shell_cmd = f"sleep 2; rm -rf {config_q}; rm -f {shim_q}"
    subprocess.Popen(
        ["sh", "-c", shell_cmd],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@app.command()
def uninstall(force: bool = typer.Option(False, "--force", "-f", help="Force uninstall without confirmation")) -> None:
    """Remove all JobCLI configuration, databases, and global shims.

    Works on Windows, macOS, and Linux. While ``wboxcli`` is running from
    ``~/.jobcli/venv``, the venv cannot delete itself; non-venv data is removed
    immediately and the rest is scheduled for removal ~2s after you exit.
    """
    import shutil

    console.print("[bold red]JobCLI Uninstall[/bold red]\n")
    if not force:
        confirm = Confirm.ask(
            "Delete all JobCLI data in ~/.jobcli (config, jobs, logs, resume, memory) and the wboxcli shims?",
            default=True,
        )
        if not confirm:
            console.print("[yellow]Uninstall cancelled.[/yellow]")
            return

    # Release SQLite/log file handles so Windows lets us delete them.
    from jobcli.utils.logger import global_logger

    global_logger.shutdown()
    _dispose_active_database()

    # Detect "are we running from inside ~/.jobcli/venv?" — if so, skip the venv
    # subtree so we don't hit "file in use" (Windows, macOS, Linux).
    is_windows = os.name == "nt"
    venv_dir = CONFIG_DIR / "venv"
    running_from_venv = False
    try:
        exe_path = Path(sys.executable).resolve()
        running_from_venv = venv_dir.resolve() in exe_path.parents
    except Exception:
        running_from_venv = False

    leftover_paths: list[Path] = []
    scheduled_post_exit = False

    # Remove the global shims first (install.ps1 / install.sh).
    bin_dir = Path.home() / ".local" / "bin"
    shim_names = ("wboxcli.cmd",) if is_windows else ("wboxcli",)
    shim_paths: list[Path] = []
    for name in shim_names:
        shim = bin_dir / name
        shim_paths.append(shim)
        if shim.exists() or shim.is_symlink():
            try:
                shim.unlink()
                console.print(f"[green]✓ Removed {shim}[/green]")
            except Exception as e:
                console.print(f"[yellow]— Could not remove {shim}: {e}[/yellow]")
                leftover_paths.append(shim)

    if CONFIG_DIR.exists():
        if running_from_venv:
            # Delete everything inside ~/.jobcli except the in-use venv subtree.
            for child in CONFIG_DIR.iterdir():
                if child.resolve() == venv_dir.resolve():
                    continue
                try:
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink()
                    console.print(f"[green]✓ Removed {child}[/green]")
                except Exception as e:
                    console.print(f"[red]✗ Failed to remove {child}: {e}[/red]")
                    leftover_paths.append(child)
            primary_shim = (
                shim_paths[0]
                if shim_paths
                else (bin_dir / "wboxcli.cmd" if is_windows else bin_dir / "wboxcli")
            )
            try:
                _schedule_post_exit_jobcli_removal(CONFIG_DIR, primary_shim)
                scheduled_post_exit = True
                console.print(
                    f"[green]✓ Scheduled removal of {CONFIG_DIR} after this process exits[/green]"
                )
                console.print(
                    "[dim]Close this terminal — the venv folder is removed automatically in ~2s.[/dim]"
                )
            except Exception as e:
                console.print(f"[yellow]— Could not schedule deferred cleanup: {e}[/yellow]")
                leftover_paths.append(venv_dir)
        else:
            try:
                _rmtree_robust(CONFIG_DIR)
                if CONFIG_DIR.exists():
                    raise OSError(f"{CONFIG_DIR} still exists")
                console.print(f"[green]✓ Removed {CONFIG_DIR}[/green]")
            except Exception as e:
                console.print(f"[red]✗ Failed to remove {CONFIG_DIR}: {e}[/red]")
                leftover_paths.append(CONFIG_DIR)
    else:
        console.print(f"[yellow]— No configuration directory at {CONFIG_DIR}[/yellow]")

    if scheduled_post_exit:
        console.print("\n[green]JobCLI uninstall scheduled — exit this terminal to finish cleanup.[/green]")
    elif leftover_paths:
        console.print()
        console.print("[yellow]Some files could not be removed while wboxcli was still running.[/yellow]")
        console.print("[yellow]Close this terminal and finish cleanup with:[/yellow]")
        if is_windows:
            console.print('  [cyan]Remove-Item -Recurse -Force "$env:USERPROFILE\\.jobcli"[/cyan]')
            console.print('  [cyan]Remove-Item -Force "$env:USERPROFILE\\.local\\bin\\wboxcli.cmd"[/cyan]')
            console.print('  [dim](or)[/dim] [cyan]irm https://raw.githubusercontent.com/WhiteboxHub/project-talentscreen-wbox-cli/dev/scripts/uninstall.ps1 | iex[/cyan]')
        else:
            console.print('  [cyan]rm -rf ~/.jobcli ~/.local/bin/wboxcli[/cyan]')
            console.print('  [dim](or)[/dim] [cyan]curl -fsSL https://raw.githubusercontent.com/WhiteboxHub/project-talentscreen-wbox-cli/dev/scripts/uninstall.sh | bash[/cyan]')
    else:
        console.print("\n[green]JobCLI has been fully uninstalled.[/green]")


@db_app.command("clear-jobs")
def db_clear_jobs(
    force: bool = typer.Option(False, "--force", "-f", help="Delete without confirmation"),
) -> None:
    """Delete all local jobs and job-scoped logs (preserves resume, config, memory)."""
    console.print("[bold cyan]Clear job data[/bold cyan]\n")
    if not force:
        if not Confirm.ask(
            "Delete all jobs and application logs from the local database?\n"
            "(Resume, field answers, learned locators, and config are kept.)",
            default=False,
        ):
            console.print("[yellow]Cancelled.[/yellow]")
            return
    db = get_database()
    session = db.get_session()
    try:
        n = JobRepository(session).clear_job_related_data()
        console.print(f"[green]✓ Removed {n} job record(s) and related logs.[/green]")
    finally:
        session.close()


# Keys cleared by ``wboxcli reset`` (jobs / field memory are kept).
_RESET_CONFIG_KEYS = (
    "job_board_username",
    "job_board_password",
    "openai_api_key",
    "anthropic_api_key",
    "gemini_api_key",
    "claude_api_key",
    "default_llm_provider",
    "resume_pdf_path",
    "resume_json_path",
    "extension_path",
    "sync_server_url",
)


def _run_reset(
    force: bool = False,
    *,
    keep_credentials: bool = False,
    keep_extension: bool = False,
) -> None:
    """Clear Whitebox login, LLM API keys, and resume (jobs and memory are kept)."""
    console.print("[bold cyan]Reset configuration[/bold cyan]\n")
    console.print(
        "[dim]Clears: Whitebox email/password, LLM API keys, resume PDF/JSON paths, "
        "and stored resume profile.[/dim]"
    )
    console.print(
        "[dim]Keeps: discovered jobs, application logs, field memory, learned locators.[/dim]"
    )
    console.print(
        "[dim]Full database wipe: [cyan]wboxcli db reset[/cyan][/dim]\n"
    )
    if not force:
        if not Confirm.ask(
            "Reset login, API keys, and resume?",
            default=False,
        ):
            console.print("[yellow]Cancelled.[/yellow]")
            return

    keys = list(_RESET_CONFIG_KEYS)
    if keep_credentials:
        keys = [k for k in keys if k not in ("job_board_username", "job_board_password", "sync_server_url")]
    if keep_extension:
        keys = [k for k in keys if k != "extension_path"]

    db = get_database()
    session = db.get_session()
    try:
        config_repo = ConfigRepository(session)
        user_repo = UserDataRepository(session)
        n_config = config_repo.delete_keys(keys)
        n_profile = user_repo.clear_profile_data()
    finally:
        session.close()

    console.print(f"[green]✓ Removed {n_config} config setting(s)[/green]")
    console.print(f"[green]✓ Cleared {n_profile} profile record(s) (resume / questions)[/green]")
    console.print(
        "\n[green]Reset complete. Run [cyan]wboxcli[/cyan] or [cyan]setup[/cyan] "
        "to enter login, LLM keys, and resume again.[/green]"
    )
    _print_next_step("setup", "re-enter login, LLM, and resume")


def _run_db_reset(force: bool) -> None:
    """Delete the SQLite DB file (+ WAL/SHM/journal) and recreate an empty schema."""
    from jobcli.sync.client import reset_sync_client_singleton
    from jobcli.storage.models import Database

    console.print("[bold red]Reset local JobCLI database[/bold red]\n")
    if not force:
        if not Confirm.ask(
            "This will delete the entire local JobCLI SQLite database, including saved login, "
            "resume data, jobs, logs, memory, learned locators, and sync metadata. Continue?",
            default=False,
        ):
            console.print("[yellow]Cancelled.[/yellow]")
            return

    db_path = resolve_active_sqlite_database_path()
    url_s = str(db_path)
    if ":memory:" in url_s:
        console.print("[red]Cannot reset an in-memory database. Point DATABASE_PATH at a file path.[/red]")
        raise typer.Exit(1)

    from jobcli.utils.logger import global_logger

    global_logger.shutdown()

    try:
        db = get_database()
        db.engine.dispose()
    except Exception:
        pass

    reset_sync_client_singleton()

    paths_to_remove = [
        db_path,
        Path(f"{db_path}-wal"),
        Path(f"{db_path}-shm"),
        Path(f"{db_path}-journal"),
    ]
    for p in paths_to_remove:
        if p.exists():
            try:
                p.unlink()
            except OSError as e:
                console.print(f"[red]Failed to delete {p}: {e}[/red]")
                raise typer.Exit(1) from e

    db_path.parent.mkdir(parents=True, exist_ok=True)
    fresh = Database(f"sqlite:///{db_path.as_posix()}")
    fresh.create_tables()
    console.print(
        "[green]Local JobCLI database reset successfully. Run [cyan]setup[/cyan] to configure credentials again.[/green]"
    )


@db_app.command("reset")
def db_reset(
    force: bool = typer.Option(False, "--force", "-f", help="Reset without confirmation"),
) -> None:
    """Delete the entire local SQLite database file and recreate empty tables.

    Does not remove ``~/.jobcli`` wholesale, log files, or resume PDFs.
    """
    _run_db_reset(force)


@app.command()
def setup() -> None:
    """One-shot setup: validate stored config, load resume from saved paths, discover, browser test.

    Run ``login`` first to save credentials (no ``.env`` file is used). Then ``setup`` will
    pick those up from local config and continue with resume, discover, and a browser test.
    """
    started_at = time.time()
    console.print("[bold cyan]╔══════════════════════════════════════╗[/bold cyan]")
    console.print("[bold cyan]║       W-BOX CLI — One-Shot Setup     ║[/bold cyan]")
    console.print("[bold cyan]╚══════════════════════════════════════╝[/bold cyan]\n")

    # ── STEP 1: Config ────────────────────────────────────────────────────────
    console.print("[bold]Step 1/5 — Verifying configuration in local store (~/.jobcli/jobcli.db)...[/bold]")
    ensure_config_dir()
    db = get_database()
    config = get_config()
    save_config(config)

    has_llm = bool(config.openai_api_key or config.anthropic_api_key or config.gemini_api_key)
    has_wbox = bool(config.job_board_username and config.job_board_password)

    if has_llm:
        console.print(f"  [green]✓ LLM provider: {config.default_llm_provider}[/green]")
    else:
        console.print("  [red]✗ No LLM API key configured.[/red]")
        console.print("    [yellow]AI form-filling will be disabled. Run [cyan]login[/cyan] and add at least one LLM key.[/yellow]")

    if has_wbox:
        api_hint = (config.sync_server_url or "default https://whitebox-learning.com/api").strip()
        console.print(f"  [green]✓ Whitebox credentials for: {config.job_board_username}[/green]")
        console.print(f"    [dim]API base: {api_hint}[/dim]")
    else:
        console.print("  [yellow]⚠ No Whitebox credentials. Run [cyan]login[/cyan].[/yellow]")
        console.print("    Job discovery will be skipped.")

    # ── STEP 2: Resume ────────────────────────────────────────────────────────
    console.print("\n[bold]Step 2/5 — Checking resume (saved paths from previous resume-upload)...[/bold]")

    pdf_path_str = config.resume_pdf_path
    json_path_str = config.resume_json_path

    resume_loaded = False
    if not pdf_path_str or not json_path_str:
        console.print("  [yellow]⚠ No resume in local config.[/yellow]")
        console.print(
            "    Run [cyan]resume-upload --pdf <file.pdf> --json <file.json>[/cyan] to load one."
        )
    else:
        pdf_path = Path(pdf_path_str.strip('"').strip("'"))
        json_path = Path(json_path_str.strip('"').strip("'"))

        if not pdf_path.exists():
            console.print(f"  [red]✗ PDF not found: {pdf_path}[/red]")
        elif not json_path.exists():
            console.print(f"  [red]✗ JSON not found: {json_path}[/red]")
        else:
            try:
                from jobcli.utils.resume_helpers import load_resume_from_paths
                from jobcli.utils.extension_resume import build_resume_file_blob

                resume, _, _, raw_json = load_resume_from_paths(
                    str(pdf_path), str(json_path)
                )
                resume_file = build_resume_file_blob(pdf_path)

                session = db.get_session()
                user_data_repo = UserDataRepository(session)
                user_data_repo.save_resume_upload_bundle(
                    resume, raw_json, resume_file
                )
                session.close()

                config.resume_pdf_path = str(pdf_path.absolute())
                config.resume_json_path = str(json_path.absolute())
                save_config(config)

                console.print(f"  [green]✓ Resume loaded: {resume.personal.first_name} {resume.personal.last_name}[/green]")
                console.print(f"    PDF : {pdf_path.name}")
                console.print(f"    JSON: {json_path.name}")
                resume_loaded = True
            except Exception as e:
                console.print(f"  [red]✗ Failed to parse resume JSON: {e}[/red]")

    # ── STEP 3: Discover Jobs ─────────────────────────────────────────────────
    console.print("\n[bold]Step 3/5 — Discovering jobs from Whitebox dashboard...[/bold]")

    jobs_found = 0
    if not has_wbox:
        console.print("  [yellow]⚠ Skipped — no Whitebox credentials. Run [cyan]login[/cyan].[/yellow]")
    else:
        try:
            session = db.get_session()
            discoverer = WboxDiscoverer(session, config=config)
            with console.status("[bold green]Fetching jobs from WBL (API)..."):
                new_jobs = discoverer.discover(headless=config.headless)
            session.close()

            jobs_found = len(new_jobs)
            if jobs_found > 0:
                console.print(f"  [green]✓ Discovered {jobs_found} new job(s) from the dashboard.[/green]")
            else:
                console.print("  [yellow]⚠ No new jobs found (they may already be in your database).[/yellow]")
        except Exception as e:
            console.print(f"  [red]✗ Job discovery failed: {e}[/red]")
            console.print("    You can run [cyan]discover[/cyan] manually later.")


    # ── STEP 4: Browser & Extension Test ─────────────────────────────────────
    console.print("\n[bold]Step 4/5 — Testing browser & extension...[/bold]")
    browser_ok = False
    extension_loaded = False
    browser_error: str = ""

    # ── 4a: Resolve & validate extension path ───────────────────────────────
    from jobcli.utils.extension_helpers import (
        get_local_extension_zip,
        maybe_install_local_extension_zip,
        resolve_extension_dir,
    )

    if get_local_extension_zip():
        maybe_install_local_extension_zip()

    ext_dir = resolve_extension_dir(config.extension_path)

    if ext_dir and ext_dir != (config.extension_path or "").strip():
        config.extension_path = ext_dir
        save_config(config)

    if not ext_dir:
        console.print("  [red]✗ Extension not found. Run the installer to clone it into bin/.[/red]")
        browser_error = "Extension not found"
    else:
        console.print(f"  [green]✓ Extension directory ready: {ext_dir}[/green]")

        # ── 4b: Launch browser with extension and verify it loaded ────────
        try:
            from playwright.sync_api import sync_playwright
            from jobcli.automation.stealth import LAUNCH_ARGS, IGNORE_DEFAULT_ARGS, CONTEXT_OPTIONS
            import tempfile

            test_url = os.getenv("WBOX_LOGIN_URL", "https://whitebox-learning.com/login")
            launch_args = list(LAUNCH_ARGS)
            launch_args.extend([
                f"--disable-extensions-except={ext_dir}",
                f"--load-extension={ext_dir}",
            ])

            with console.status("[bold green]Launching browser with extension (this may take ~15s)..."):
                with sync_playwright() as pw:
                    user_data_dir = tempfile.mkdtemp(prefix="jobcli_setup_test_")
                    ctx = pw.chromium.launch_persistent_context(
                        user_data_dir,
                        headless=False,
                        args=launch_args,
                        ignore_default_args=IGNORE_DEFAULT_ARGS,
                        **CONTEXT_OPTIONS,
                    )

                    page = ctx.new_page()
                    response = page.goto(test_url, timeout=30000, wait_until="domcontentloaded")
                    status_code = response.status if response else 0
                    page_title = page.title() or "(no title)"

                    # Check extension is actually attached via service workers / background pages
                    try:
                        sw = list(ctx.service_workers)
                        bp = list(ctx.background_pages)
                        extension_loaded = bool(sw or bp)
                    except Exception:
                        extension_loaded = False

                    console.print("\n  [cyan]Holding browser open for 15 seconds for visual inspection...[/cyan]")
                    page.wait_for_timeout(15000)
                    ctx.close()

            if status_code and status_code < 400:
                browser_ok = True
                if extension_loaded:
                    console.print(f"  [green]✓ Extension loaded and verified successfully: {Path(ext_dir).name}[/green]")
                else:
                    console.print(f"  [yellow]⚠ Extension directory found, but Playwright could not verify its background service. It may load on the first application.[/yellow]")
                console.print(f"  [green]✓ Test URL reachable ({test_url}) — HTTP {status_code}[/green]")
                console.print(f"    Page title: {page_title}")
            else:
                browser_error = f"HTTP {status_code} from {test_url}"
                console.print(f"  [red]✗ Test URL returned error status: {browser_error}[/red]")

        except Exception as _be:
            browser_error = str(_be)
            console.print(f"  [red]✗ Browser launch failed: {browser_error}[/red]")
            console.print("    Make sure Playwright is installed: [cyan]playwright install chromium[/cyan]")

    # ── STEP 5: Summary ───────────────────────────────────────────────────────
    console.print("\n[bold]Step 5/5 — Summary[/bold]")
    console.print("─" * 45)
    console.print(f"  Config saved   : [green]✓[/green]  {CONFIG_FILE}")
    console.print(f"  LLM ready      : {'[green]✓[/green]' if has_llm else '[red]✗[/red]'}")
    console.print(f"  Resume loaded  : {'[green]✓[/green]' if resume_loaded else '[yellow]⚠ skipped[/yellow]'}")
    console.print(f"  Jobs found     : [green]{jobs_found}[/green] new job(s)")
    console.print(f"  Browser test   : {'[green]✓ PASSED[/green]' if browser_ok else '[red]✗ FAILED[/red]'}")
    if extension_loaded:
        console.print(f"  Extension      : [green]✓ loaded[/green]")
    console.print("─" * 45)

    if has_llm and resume_loaded and browser_ok:
        console.print("\n[bold green]✓ Setup complete! You are ready to apply.[/bold green]")
        _print_next_step("discover", "pull listings, then run apply")
    else:
        console.print("\n[bold yellow]⚠ Setup finished with warnings. Fix the issues above, then re-run setup.[/bold yellow]")
        if not resume_loaded:
            _print_next_step("resume-upload --pdf <file.pdf> --json <file.json>", "load your resume")
        else:
            _print_next_step("setup", "re-run after fixing the issues above")
    _track_command_event(
        db=db,
        config=config,
        command="setup",
        started_at=started_at,
        result="success" if (has_llm and resume_loaded and browser_ok) else "warning",
        metadata={"jobs_found": jobs_found},
    )


@app.command()
def login(
    auto: bool = typer.Option(False, "--auto", help="Skip prompts if credentials already saved in local config"),
) -> None:
    """Configure credentials for job boards and LLM APIs."""
    started_at = time.time()
    console.print("[bold cyan]JobCLI Login[/bold cyan]\n")

    config = get_config()
    
    if auto and config.job_board_username and (config.openai_api_key or config.anthropic_api_key or config.gemini_api_key):
        console.print("[green]✓ Credentials already saved in local config. Skipping manual entry.[/green]")
        _track_command_event(
            db=get_database(),
            config=config,
            command="login",
            started_at=started_at,
            result="success",
            metadata={"auto": True},
        )
        return

    # Job board credentials
    console.print("[bold]Job Board Credentials[/bold]")
    job_board_username = Prompt.ask(
        "whitebox-learning.com username",
        default=config.job_board_username or "",
    )
    job_board_password = Prompt.ask(
        "whitebox-learning.com password",
        password=True,
    )

    if job_board_username:
        config.job_board_username = job_board_username
    if job_board_password:
        config.job_board_password = job_board_password

    # Auto-detect the WBL API base URL. Only one endpoint is probed:
    #   - https://whitebox-learning.com/api   (production)
    # Probe it with the supplied credentials and save it if authentication
    # succeeds.
    # If the probe uncovers actionable issues (bad creds, TLS) we surface a
    # concise warning.
    from jobcli.sync.client import (
        probe_wbl_api_detailed,
        WBL_API_CANDIDATES,
        LOGIN_ERR_BAD_CREDENTIALS,
        LOGIN_ERR_ACCOUNT_LOCKED,
        LOGIN_ERR_SSL,
        LOGIN_ERR_RATE_LIMIT,
    )

    probe_warning_lines: list[str] = []
    if job_board_username and job_board_password:
        winning, probe_errors = probe_wbl_api_detailed(job_board_username, job_board_password)
        config.sync_server_url = winning or WBL_API_CANDIDATES[0]
        if not winning and probe_errors:
            kinds = {e["kind"] for e in probe_errors}
            # Only flag what the user can act on right now. Pure network errors
            # (local backend down, no VPN) stay silent — login still saves
            # credentials and discover retries on demand.
            if LOGIN_ERR_BAD_CREDENTIALS in kinds:
                probe_warning_lines.append(
                    "WBL did not accept these credentials. Re-run 'wboxcli login' "
                    "with the correct email/password — discover will fail otherwise."
                )
            if LOGIN_ERR_ACCOUNT_LOCKED in kinds:
                probe_warning_lines.append(
                    "Your WBL account appears inactive/locked. Contact Recruiting "
                    "before running 'wboxcli discover'."
                )
            if LOGIN_ERR_SSL in kinds:
                probe_warning_lines.append(
                    "TLS certificate verification failed for the WBL API. "
                    "Set JOBCLI_SSL_CA_BUNDLE=<path-to-ca.pem> (preferred) or "
                    "JOBCLI_INSECURE_TLS=1 (insecure) before 'jobcli discover'."
                )
            if LOGIN_ERR_RATE_LIMIT in kinds:
                probe_warning_lines.append(
                    "WBL rate-limited the login probe. Wait a minute, then run "
                    "'wboxcli discover' — credentials were saved successfully."
                )
    else:
        # No creds yet — save the production URL so SyncClient has a starting point.
        if not (config.sync_server_url or "").strip():
            config.sync_server_url = WBL_API_CANDIDATES[0]

    # LLM API keys
    console.print("\n[bold]LLM API Keys (optional)[/bold]")
    console.print("At least one LLM provider is recommended for better automation.")

    openai_key = Prompt.ask(
        "OpenAI API key",
        default=config.openai_api_key or "",
        show_default=False,
    )
    anthropic_key = Prompt.ask(
        "Anthropic API key",
        default=config.anthropic_api_key or "",
        show_default=False,
    )
    gemini_key = Prompt.ask(
        "Google Gemini API key",
        default=config.gemini_api_key or "",
        show_default=False,
    )

    if openai_key:
        config.openai_api_key = openai_key
    if anthropic_key:
        config.anthropic_api_key = anthropic_key
    if gemini_key:
        config.gemini_api_key = gemini_key

    # Default LLM provider
    if openai_key or anthropic_key or gemini_key:
        provider = Prompt.ask(
            "Default LLM provider",
            choices=["openai", "anthropic", "gemini"],
            default=config.default_llm_provider,
        )
        config.default_llm_provider = provider  # type: ignore

    # Save config
    save_config(config)

    console.print("\n[bold green]✓ Credentials saved successfully[/bold green]")
    for line in probe_warning_lines:
        console.print(f"[yellow]⚠ {line}[/yellow]")
    cmd, hint = _suggest_after_login_or_resume()
    _print_next_step(cmd, hint)
    _track_command_event(
        db=get_database(),
        config=config,
        command="login",
        started_at=started_at,
        result="success",
        metadata={"warnings": len(probe_warning_lines)},
    )


@app.command(name="config")
def config_cmd(
    key: Optional[str] = typer.Option(None, "--key", "-k", help="Configuration key to view or set"),
    set_value: Optional[str] = typer.Option(None, "--set", help="Set configuration value"),
) -> None:
    """View or modify configuration.

    Use ``sync_server_url`` for the WBL API base, e.g.:

        wboxcli config --key sync_server_url --set https://whitebox-learning.com/api
    """
    config = get_config()

    resolved_key = CONFIG_CMD_KEY_ALIASES.get(key, key) if key else None

    if key and set_value:
        # Set configuration
        rk = resolved_key or key
        if rk and hasattr(config, rk):
            setattr(config, rk, set_value)
            save_config(config)
            console.print(f"[green]✓ Set {rk} = {set_value}[/green]")
        else:
            console.print(f"[red]Unknown configuration key: {key}[/red]")

    elif key:
        # View specific key
        rk = resolved_key or key
        if rk and hasattr(config, rk):
            value = getattr(config, rk)
            console.print(f"{rk}: {value}")
        else:
            console.print(f"[red]Unknown configuration key: {key}[/red]")

    else:
        # View all configuration
        table = Table(title="JobCLI Configuration")
        table.add_column("Key", style="cyan")
        table.add_column("Value", style="green")

        config_dict = config.model_dump()
        for k, v in config_dict.items():
            # Hide sensitive values
            if "key" in k.lower() or "password" in k.lower():
                if v:
                    v = "***" + str(v)[-4:] if len(str(v)) > 4 else "***"
            table.add_row(k, str(v))

        console.print(table)


def _clean_path_input(raw: Optional[str]) -> Optional[str]:
    """Strip surrounding quotes / whitespace that Windows shells often leave on
    pasted paths. Saves us from the classic ``C:\\Users\\sampa\\"C:\\..."``
    bug where the literal ``"`` becomes part of the stored path string.

    Handles three cases observed in the wild:
      1. ``"C:\\Users\\sam\\resume.pdf"`` → ``C:\\Users\\sam\\resume.pdf``
      2. ``'C:\\Users\\sam\\resume.pdf'`` → ``C:\\Users\\sam\\resume.pdf``
      3. Trailing whitespace from interactive paste.
    """
    if raw is None:
        return None
    s = raw.strip()
    # PowerShell tab-completion sometimes wraps paths with spaces in quotes
    # and the shell passes the quotes through to Python as part of argv.
    for quote in ('"', "'"):
        if len(s) >= 2 and s.startswith(quote) and s.endswith(quote):
            s = s[1:-1].strip()
            break
    return s or None


@app.command()
def resume_upload(
    pdf: str = typer.Option(..., help="Path to resume PDF"),
    json_file: Optional[str] = typer.Option(None, "--json", help="Path to resume JSON (optional)"),
) -> None:
    """Upload resume in PDF format (JSON is optional)."""
    console.print("[bold cyan]Resume Upload[/bold cyan]\n")

    # Strip surrounding quotes that PowerShell / cmd often pass through when
    # a path is pasted. Without this, a user typing
    # ``--pdf "C:\Users\sam\resume.pdf"`` would have the literal quotes
    # stored in the DB, and downstream code (which prepends CWD to relative
    # paths) would produce errors like
    # ``C:\Users\sampa\"C:\Users\sampa\Downloads\resume.pdf"``.
    from jobcli.utils.resume_helpers import (
        clean_path_input,
        load_resume_from_paths,
        persist_resume,
        print_profile_summary,
    )

    pdf = clean_path_input(pdf) or ""
    json_file = clean_path_input(json_file)

    if not json_file:
        pdf_path = Path(pdf).expanduser().resolve()
        if not pdf_path.exists():
            console.print(f"[red]PDF file not found: {pdf_path}[/red]")
            raise typer.Exit(1)
        json_path = pdf_path.with_suffix(".json")
        if not json_path.exists():
            console.print("[yellow]No JSON provided. Creating a basic profile from your login info...[/yellow]")
            config = get_config()
            minimal_resume = {
                "personal": {
                    "first_name": config.job_board_username.split("@")[0] if config.job_board_username else "User",
                    "last_name": "Applicant",
                    "email": config.job_board_username or "user@example.com",
                    "phone": "",
                    "location": "",
                    "linkedin": "",
                    "github": "",
                    "website": "",
                },
                "education": [],
                "experience": [],
                "skills": [],
                "projects": [],
                "summary": "Professional applicant.",
            }
            json_path.write_text(json.dumps(minimal_resume, indent=2))
        json_file = str(json_path)

    try:
        resume, pdf_path, json_path, raw_json = load_resume_from_paths(pdf, json_file)
        print_profile_summary(console, resume, pdf_path)
        persist_resume(resume, pdf_path, json_path, raw_json_resume=raw_json)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Invalid resume JSON: {e}[/red]")
        raise typer.Exit(1)

    console.print("\n[bold green]✓ Resume uploaded successfully[/bold green]")
    cmd, hint = _suggest_after_login_or_resume()
    _print_next_step(cmd, hint)




@app.command()
def questions() -> None:
    """Pre-fill answers to common application questions."""
    try:
        console.print("[bold cyan]Common Application Questions[/bold cyan]\n")

        # Load existing answers if any
        db = get_database()
        session = db.get_session()
        user_data_repo = UserDataRepository(session)

        existing = user_data_repo.get_questions()

        questions_data = CommonQuestions()

        # Prompt for each question
        questions_data.salary_expectations = Prompt.ask(
            "Salary expectations",
            default=existing.salary_expectations if existing else "",
            show_default=True,
        )

        questions_data.notice_period = Prompt.ask(
            "Notice period",
            default=existing.notice_period if existing else "",
        )

        willing_relocate = Confirm.ask(
            "Willing to relocate?",
            default=existing.willing_to_relocate if existing else None,
        )
        questions_data.willing_to_relocate = willing_relocate

        remote_default = existing.remote_preference if existing else "flexible"
        questions_data.remote_preference = Prompt.ask(
            "Remote work preference",
            choices=["remote", "hybrid", "onsite", "flexible"],
            default=remote_default,
        )

        questions_data.start_date = Prompt.ask(
            "Available start date",
            default=existing.start_date if existing else "",
        )

        questions_data.referral = Prompt.ask(
            "Referral (if any)",
            default=existing.referral if existing else "",
        )

        # Save
        user_data_repo.save_questions(questions_data)
        session.close()

        console.print("\n[bold green]✓ Answers saved successfully[/bold green]")
    except Exception:
        raise


def _run_apply(
    *,
    url: Optional[str],
    limit: Optional[int],
    sort: str,
    mode: str,
    job_ids: Optional[list[int]] = None,
    ats: Optional[str] = None,
    resume: bool = False,
    skip_resume_prompt: bool = False,
) -> None:
    """Shared implementation for ``wboxcli apply`` (single URL or batch).

    Exit semantics
    --------------
    The user can quit at any interactive prompt by typing one of
    ``q``/``quit``/``exit``/``:q`` or pressing **Ctrl+C** (twice in 2s to
    force-quit). Both raise :class:`ExitRequested`, which we catch here to
    close the browser cleanly, persist learned state, and print a tally —
    distinct from an unexpected crash. This is wired uniformly across
    every prompt the engine exposes (handoff, submit confirm, ASK
    actions, LinkedIn-skip yes/no, manual pauses) via
    ``AgentInterface._get_user_input``.

    After an interrupted batch, the CLI saves a checkpoint and asks whether
    to resume (yes/no). ``wboxcli continue`` or ``wboxcli apply --continue``
    skips the prompt and continues the same queue.
    """
    from jobcli.utils.apply_checkpoint import (
        clear_apply_checkpoint,
        jobs_from_checkpoint,
        load_apply_checkpoint,
        reset_interrupted_job_to_pending,
        save_apply_checkpoint,
    )
    from jobcli.utils.exit_signal import (
        ExitRequested,
        install_global_sigint_handler,
        is_exit_requested,
        reset_exit_flag,
    )

    started_at = time.time()
    console.print("[bold cyan]Job Application[/bold cyan]\n")

    # Install the SIGINT handler before we touch any blocking primitive so
    # Ctrl+C in PowerShell wakes us cleanly instead of leaving daemon-thread
    # input() calls dangling. Safe to call multiple times.
    install_global_sigint_handler()

    config = get_config()
    ensure_configured(config, require_job_board=False)

    try:
        config.interaction_mode = InteractionMode(mode)
    except ValueError:
        console.print(f"[red]Invalid mode '{mode}'. Choose from: auto, supervised, manual[/red]")
        raise typer.Exit(1)

    console.print(f"Mode: [cyan]{config.interaction_mode.value}[/cyan]")
    console.print(
        "[dim]Press [bold]Ctrl+C[/bold] or type [bold]q[/bold] / [bold]quit[/bold] / "
        "[bold]exit[/bold] at any prompt to stop cleanly. Two Ctrl+C within 2s force quits.[/dim]\n"
        "[dim]After a stop you can resume with [bold]wboxcli continue[/bold] or answer "
        "[bold]yes[/bold] when prompted.[/dim]\n"
    )

    db = get_database()
    session = db.get_session()

    user_data_repo = UserDataRepository(session)
    job_repo = JobRepository(session)

    checkpoint = None
    if resume and not url:
        checkpoint = load_apply_checkpoint(session)
        if not checkpoint:
            console.print(
                "[yellow]No interrupted apply run found. Run [cyan]discover[/cyan] then "
                "[cyan]apply[/cyan], or press Ctrl+C during apply to save a checkpoint.[/yellow]"
            )
            session.close()
            raise typer.Exit(0)
        mode = checkpoint.mode
        sort = checkpoint.sort
        limit = checkpoint.limit
        try:
            config.interaction_mode = InteractionMode(mode)
        except ValueError:
            pass
        console.print(
            f"[cyan]Resuming interrupted run[/cyan] "
            f"([bold]{checkpoint.remaining_count}[/bold] job(s) remaining)…\n"
        )
        reset_exit_flag()

    resume_data = user_data_repo.get_resume()
    if not resume_data:
        console.print(
            "[red]No resume in the local database.[/red] Run "
            "[cyan]resume-upload --pdf <file.pdf> --json <file.json>[/cyan]."
        )
        raise typer.Exit(1)

    jobs = []

    if url:
        job = job_repo.get_by_url(url)
        if not job:
            job = job_repo.create(
                Job(title="Manual Entry", url=url, status=ApplicationStatus.PENDING)
            )
        if job.status != ApplicationStatus.PENDING:
            job_repo.update_status(job.id, ApplicationStatus.PENDING)
        jobs = [job]

    else:
        if checkpoint:
            jobs = jobs_from_checkpoint(session, checkpoint)
        else:
            # Resolve selected IDs from the pending set only. A stale command
            # therefore cannot re-apply a job which was already submitted.
            pending_by_id = {job.id: job for job in job_repo.list_pending()}
            jobs = (
                [pending_by_id[job_id] for job_id in job_ids if job_id in pending_by_id]
                if job_ids
                else list(pending_by_id.values())
            )
            if sort.lower() == "newest":
                jobs.reverse()
            if limit:
                jobs = jobs[:limit]

        if not jobs:
            if checkpoint:
                clear_apply_checkpoint(session)
                console.print("[yellow]No remaining jobs in the saved checkpoint (already finished).[/yellow]")
            else:
                console.print("[yellow]No pending jobs found. Run [cyan]discover[/cyan] first.[/yellow]")
            session.close()
            raise typer.Exit(0)

    # Discovery happens before ATS detection, so use both the stored ATS type
    # and the final listing URL when filtering Whitebox jobs.
    if ats:
        normalized_ats = ats.strip().lower()
        if normalized_ats != "ashby":
            session.close()
            console.print("[red]Only the Ashby filter is currently supported: --ats ashby[/red]")
            raise typer.Exit(1)
        jobs = [
            job for job in jobs
            if getattr(job.ats_type, "value", str(job.ats_type)).lower() == "ashby"
            or "ashbyhq.com" in job.url.lower()
        ]

    original_count = len(jobs)
    jobs = [
        j
        for j in jobs
        if any(d in j.url.lower() for d in SUPPORTED_DOMAINS)
        and "myworkdayjobs.com" not in j.url.lower()
    ]

    if len(jobs) < original_count:
        console.print(f"[yellow]Filtered out {original_count - len(jobs)} unsupported jobs (Workday, etc.).[/yellow]")

    if not jobs:
        suffix = " for the selected Ashby filter" if ats else ""
        console.print(f"[yellow]No supported pending jobs remaining{suffix}.[/yellow]")
        session.close()
        return

    console.print(f"Applying to {len(jobs)} job(s)...\n")

    engine = ApplicationEngine(config, resume_data, db)
    tracker = SessionTracker(backend_url=config.sync_server_url or "https://whitebox-learning.com/api")


    if checkpoint:
        full_job_ids = list(checkpoint.job_ids)
        batch_start_index = checkpoint.next_index
    else:
        full_job_ids = [j.id for j in jobs if j.id is not None]
        batch_start_index = 0

    # Tally counters so the summary panel is meaningful regardless of how
    # we exit (clean finish, ExitRequested, or unexpected crash).
    submitted = 0
    skipped = 0
    failed = 0
    processed = 0
    processed_job_ids: list[int] = []
    exit_reason: Optional[str] = None
    interrupted_at_index: Optional[int] = None
    interrupted_job_id: Optional[int] = None

    try:
        try:
            engine.start_session()
            for i, job in enumerate(jobs, 1):
                # Allow Ctrl+C between jobs to exit before opening a new tab.
                if is_exit_requested():
                    exit_reason = "Ctrl+C between jobs"
                    interrupted_at_index = batch_start_index + (i - 1)
                    interrupted_job_id = job.id
                    break
                console.print(f"[bold]Job {i}/{len(jobs)}[/bold]: {job.url}")
                if job.id is not None:
                    job_repo.update_status(job.id, ApplicationStatus.IN_PROGRESS)
                try:
                    status = engine.apply_to_job(job)
                    console.print(f"Status: [green]{status.value}[/green]\n")
                    processed += 1
                    if job.id is not None:
                        processed_job_ids.append(job.id)
                    # Match on the enum value to avoid importing every status.
                    sv = getattr(status, "value", str(status)).lower()
                    if "submit" in sv or "success" in sv or "complete" in sv:
                        submitted += 1
                        _ext   = getattr(engine, "_last_extension_filled_count", 0)
                        _rules = getattr(engine, "_last_rules_filled_count", 0)
                        _llm   = getattr(engine, "_last_llm_filled_count", 0)
                        _human = getattr(engine, "_last_human_filled_count", 0)
                        # Resolve real candidate name from stored resume; fall back to email.
                        _candidate_name = config.job_board_username or "Unknown"
                        try:
                            from jobcli.storage.repositories import UserDataRepository as _UDR
                            _udr = _UDR(session)
                            _resume = _udr.get_resume()
                            if _resume and _resume.personal:
                                _fn = (_resume.personal.first_name or "").strip()
                                _ln = (_resume.personal.last_name or "").strip()
                                _full = f"{_fn} {_ln}".strip()
                                if _full:
                                    _candidate_name = _full
                        except Exception:
                            pass
                        tracker.add_application(
                            _candidate_name,
                            job.company or "Unknown",
                            getattr(job.ats_type, 'value', 'unknown'),
                            {
                                "autofill_fields": _ext,
                                "llm_fields":      _rules + _llm,
                                "human_fields":    _human,
                                "total_fields":    _ext + _rules + _llm + _human,
                            }
                        )
                        # Send analytics instantly to the backend to prevent data loss on crash/force-quit
                        tracker.send_bulk_summary()
                    elif "skip" in sv or "cancel" in sv:
                        skipped += 1
                    else:
                        failed += 1
                except ExitRequested as ex:
                    exit_reason = ex.reason
                    interrupted_at_index = batch_start_index + (i - 1)
                    interrupted_job_id = job.id
                    break
                except BrowserClosed:
                    exit_reason = "Browser closed by user"
                    interrupted_at_index = batch_start_index + (i - 1)
                    interrupted_job_id = job.id
                    processed += 1
                    if job.id is not None:
                        processed_job_ids.append(job.id)
                    console.print(
                        "\n[yellow]Chrome was closed — saving apply logs and uploading analytics…[/yellow]\n"
                    )
                    break
                except KeyboardInterrupt:
                    # Fallback path if the SIGINT handler wasn't installed
                    # (e.g. running under an embedded interpreter). Treated
                    # identically to an ExitRequested.
                    exit_reason = "Ctrl+C (KeyboardInterrupt)"
                    interrupted_at_index = batch_start_index + (i - 1)
                    interrupted_job_id = job.id
                    break
                except Exception as e:
                    console.print(f"[red]Error: {e}[/red]\n")
                    failed += 1
                    processed += 1
                    if job.id is not None:
                        processed_job_ids.append(job.id)
                    continue
        except ExitRequested as ex:
            # ExitRequested raised by ``engine.start_session()`` (i.e. user
            # quit during browser startup) — handled identically to the
            # per-job path, just no jobs processed.
            exit_reason = ex.reason
        except BrowserClosed:
            exit_reason = "Browser closed by user"
        except KeyboardInterrupt:
            exit_reason = "Ctrl+C during startup"
    finally:
        # ``stop_session`` closes the Playwright browser context. Guarded
        # because a Ctrl+C during start_session() leaves no browser to
        # close and we don't want the cleanup itself to throw.
        try:
            engine.stop_session()
        except Exception as e:
            console.print(f"[dim red]Warning: error during session shutdown: {e}[/dim red]")
        tracker.send_bulk_summary()

    # -----------------------------------------------------------------
    # Checkpoint + resume prompt (batch runs only)
    # -----------------------------------------------------------------
    if exit_reason and not url and full_job_ids and interrupted_at_index is not None:
        reset_interrupted_job_to_pending(session, interrupted_job_id)
        save_apply_checkpoint(
            session,
            job_ids=full_job_ids,
            next_index=interrupted_at_index,
            mode=config.interaction_mode.value,
            sort=sort,
            limit=limit,
        )
        remaining = max(0, len(full_job_ids) - interrupted_at_index)
    elif not exit_reason and not url and full_job_ids:
        clear_apply_checkpoint(session)
        remaining = 0
    else:
        remaining = 0

    session.close()

    # -----------------------------------------------------------------
    # Final summary — always rendered, branded for both clean-finish and
    # user-initiated exit so the operator immediately sees what got done.
    # -----------------------------------------------------------------
    from rich.panel import Panel
    tally = (
        f"Processed: [bold]{processed}[/bold] of {len(jobs)}\n"
        f"Submitted: [green]{submitted}[/green]   "
        f"Skipped: [yellow]{skipped}[/yellow]   "
        f"Failed: [red]{failed}[/red]"
    )
    if exit_reason:
        resume_hint = ""
        if remaining:
            resume_hint = (
                f"\n\n[dim]Resume later:[/dim] [cyan]wboxcli continue[/cyan] "
                f"or [cyan]wboxcli apply --continue[/cyan]"
            )
        console.print(
            Panel(
                f"[yellow]Exit requested:[/yellow] {exit_reason}\n\n{tally}{resume_hint}",
                title="[bold yellow]>>> JobCLI stopped at your request <<<[/bold yellow]",
                border_style="yellow",
                expand=True,
            )
        )
    else:
        console.print(
            Panel(
                tally,
                title="[bold green]>>> Application run complete <<<[/bold green]",
                border_style="green",
                expand=True,
            )
        )

    from jobcli.cli.upload_pipeline import (
        ApplyUploadContext,
        DEFAULT_BACKFILL_HOURS,
        print_upload_status,
        run_post_apply_upload,
    )

    db = get_database()
    track_result = (
        "browser_closed"
        if exit_reason and "browser" in (exit_reason or "").lower()
        else ("interrupted" if exit_reason else "success")
    )
    upload_result = run_post_apply_upload(
        db,
        config,
        apply_context=ApplyUploadContext(
            started_at=started_at,
            result=track_result,
            jobs_attempted_count=processed,
            jobs_submitted_count=submitted,
            jobs_failed_count=failed,
            processed_job_ids=processed_job_ids,
            exit_reason=exit_reason,
        ),
        since_hours=DEFAULT_BACKFILL_HOURS,
        console=console,
    )
    print_upload_status(upload_result, console=console)

    if exit_reason:
        if remaining and not skip_resume_prompt:
            reset_exit_flag()
            console.print(
                f"\n[bold]You stopped with {remaining} job(s) left in this batch.[/bold]"
            )
            if Confirm.ask(
                "Resume applications where you left off?",
                default=True,
            ):
                _run_apply(
                    url=None,
                    limit=limit,
                    sort=sort,
                    mode=config.interaction_mode.value,
                    resume=True,
                    skip_resume_prompt=True,
                )
                return
            console.print(
                "[dim]To resume later, run [cyan]wboxcli continue[/cyan].[/dim]"
            )
        _print_apply_run_summary_table(processed_job_ids, track_result=track_result)
        raise typer.Exit(0)

    _print_apply_run_summary_table(processed_job_ids, track_result="success")
    _print_next_step("discover", "refresh listings, then run apply again")


@app.command()
def apply(
    job_url: Optional[str] = typer.Argument(None, help="Single job URL to apply (positional)"),
    url: Optional[str] = typer.Option(None, "--url", "-u", help="Single job URL to apply (legacy flag)"),
    batch: bool = typer.Option(
        False,
        "--batch",
        help="[Deprecated] No effect — `wboxcli apply` already applies to all pending jobs when no --url is given.",
    ),
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Limit number of jobs when applying to many"),
    sort: str = typer.Option("oldest", "--sort", "-s", help="Sort when applying to many: oldest | newest"),
    mode: str = typer.Option(
        "supervised",
        "--mode", "-m",
        help="Interaction mode: auto (fully autonomous), supervised (AI + human checkpoints, default), manual (pause at every step)",
    ),
    continue_run: bool = typer.Option(
        False,
        "--continue",
        "-c",
        help="Resume the last batch apply stopped with Ctrl+C (same as ``wboxcli continue``).",
    ),
    job_id: Optional[list[int]] = typer.Option(
        None,
        "--job-id",
        "-j",
        help="Apply only these pending job IDs. Repeat for each selected job.",
    ),
    ats: Optional[str] = typer.Option(
        None,
        "--ats",
        help="Filter pending jobs by ATS. Currently supported: ashby.",
    ),
) -> None:
    """Apply to all pending jobs from ``discover`` (or one job with ``--url``).

    Flow: ``login`` → ``resume-upload`` → ``discover`` → ``apply``.

    Modes: **auto** (minimal stops), **supervised** (default), **manual** (approve each batch).

    Source filtering happens at ``discover`` time — the local DB only ever
    contains links from the allow-list (see ``jobcli/orchestration/source_filter.py``),
    so ``apply`` simply iterates whatever is pending.
    """
    _ = batch  # accepted for backward compatibility with scripts using ``--batch``
    _run_apply(
        url=job_url or url,
        limit=limit,
        sort=sort,
        mode=mode,
        job_ids=job_id,
        ats=ats,
        resume=continue_run,
        skip_resume_prompt=continue_run,
    )


@app.command("continue")
def continue_apply() -> None:
    """Resume the last apply batch stopped with Ctrl+C or ``q``/``quit``."""
    config = get_config()
    _run_apply(
        url=None,
        limit=None,
        sort="oldest",
        mode=config.interaction_mode.value if config.interaction_mode else "supervised",
        job_ids=None,
        ats=None,
        resume=True,
        skip_resume_prompt=True,
    )


@app.command()
def discover(
    headless: bool = typer.Option(False, help="Run browser in headless mode (legacy UI mode only)"),
    legacy_ui: bool = typer.Option(
        False,
        "--legacy-ui",
        help="Use Playwright user_dashboard scrape instead of WBL API (also: WBOX_DISCOVER_MODE=browser)",
    ),
) -> None:
    """Discover jobs from Whitebox Learning via API (GET /positions/cli_window).

    After login, local jobs are replaced with a mirror of the server listing set
    (default: last 7 UTC days, ``open`` only, newest-first), paged until every
    row is fetched — same data model as the dashboard Jobs grid. Tune with
    ``JOBCLI_DISCOVER_DAYS`` (``0`` = all time), ``JOBCLI_DISCOVER_PAGE_SIZE``,
    ``JOBCLI_DISCOVER_STATUS``. Use ``--legacy-ui`` only for the old dashboard scraper.
    """
    started_at = time.time()
    console.print("[bold cyan]Job Discovery[/bold cyan]\n")

    config = get_config()
    _require_wbl_credentials_for_discovery(config)
    ensure_configured(config)

    db = get_database()
    session = db.get_session()

    try:
        discoverer = WboxDiscoverer(session, config=config)
        with console.status("[bold green]Fetching jobs from WBL API..."):
            new_jobs = discoverer.discover(headless=headless, legacy_ui=legacy_ui)

        print_dashboard_summary(session)

        if new_jobs:
            console.print(f"\n[bold green]✓ Imported {len(new_jobs)} job(s) from WBL.[/bold green]")
            _print_next_step("apply", "start applying to pending jobs")
        else:
            console.print("\n[yellow]No jobs imported (empty window or missing credentials).[/yellow]")
            _print_next_step("login", "verify credentials & API base URL, then re-run discover")
        _track_command_event(
            db=db,
            config=config,
            command="discover",
            started_at=started_at,
            result="success",
            metadata={"jobs_imported": len(new_jobs)},
        )

    except Exception as e:
        msg = str(e)
        # When SyncClient produced its own classified, multi-line "Next step:"
        # block, render it as-is without the generic panel below it.
        if "Authentication failed against all WBL API candidates" in msg:
            console.print("\n[red]Discovery failed: WBL login failed.[/red]")
            for line in msg.splitlines()[1:]:  # skip the leading "WBL login failed." line
                console.print(line)
        else:
            console.print(f"\n[red]Discovery failed: {e}[/red]")
            _print_next_step("login", "fix credentials / API base URL, then re-run discover")
        _track_command_event(
            db=db,
            config=config,
            command="discover",
            started_at=started_at,
            result="error",
        )
    finally:
        session.close()


@app.command()
def open_dashboard() -> None:
    """Open Whitebox Learning dashboard in an interactive browser window."""
    console.print("[bold cyan]Opening Dashboard[/bold cyan]\n")
    
    config = get_config()
    _require_wbl_credentials_for_discovery(config)
    ensure_configured(config)
    
    db = get_database()
    session = db.get_session()
    
    try:
        discoverer = WboxDiscoverer(session, config=config)
        discoverer.open_interactive()
    except KeyboardInterrupt:
        console.print("\n[yellow]Browser closed.[/yellow]")
    except Exception as e:
        console.print(f"\n[red]Failed to open dashboard: {e}[/red]")
    finally:
        session.close()


@app.command()
def scan(
    portal_config: str = typer.Option(
        "config/portals.example.yml",
        "--config",
        "-c",
        help="Path to portals configuration YAML",
    )
) -> None:
    """Scan configured ATS portals for open jobs."""
    console.print("[bold cyan]ATS Zero-Token Scanner[/bold cyan]\n")
    
    import yaml
    from jobcli.orchestration.scanner import ATSScanner
    from jobcli.storage.repositories import JobRepository
    
    try:
        with open(portal_config, "r") as f:
            config_data = yaml.safe_load(f)
    except Exception as e:
        console.print(f"[red]Error loading config: {e}[/red]")
        return
        
    portals = config_data.get("portals", [])
    if not portals:
        console.print("[yellow]No portals configured to scan.[/yellow]")
        return
        
    scanner = ATSScanner()
    db = get_database()
    session = db.get_session()
    job_repo = JobRepository(session)
    
    total_added = 0
    with console.status("[bold green]Scanning portals..."):
        for portal in portals:
            name = portal.get("name")
            ats = portal.get("ats")
            company_id = portal.get("company_id")
            
            if not all([name, ats, company_id]):
                continue
                
            console.print(f"Scanning {name} ({ats})...")
            
            jobs = []
            if ats == "greenhouse":
                jobs = scanner.scan_greenhouse(company_id)
            elif ats == "lever":
                jobs = scanner.scan_lever(company_id)
            elif ats == "ashby":
                jobs = scanner.scan_ashby(company_id)
            elif ats == "bamboo_hr":
                jobs = scanner.scan_bamboohr(company_id)
                
            added = 0
            for job in jobs:
                if not job_repo.get_by_url(job.url):
                    job_repo.create(job)
                    added += 1
            
            if added:
                console.print(f"  [green]✓ Found {added} new jobs![/green]")
            total_added += added
            
    session.close()
    console.print(f"\n[bold green]Scan complete. Added {total_added} new jobs to database.[/bold green]")


@app.command("log")
def log_cmd(
    job_id: Optional[int] = typer.Option(
        None,
        "--job-id",
        "-j",
        help="Show raw application log lines for one job instead of the latest apply-run table",
    ),
    tail: int = typer.Option(60, "--tail", "-n", help="Number of log lines when using --job-id"),
    urls: bool = typer.Option(False, "--urls", help="Print job URLs under the latest apply-run table"),
) -> None:
    """Show latest applied jobs (table) or tail logs for one job (--job-id)."""
    _run_log(job_id=job_id, tail=tail, show_urls=urls)


@app.command("logs")
def logs_cmd(
    job_id: Optional[int] = typer.Option(None, "--job-id", "-j"),
    tail: int = typer.Option(60, "--tail", "-n"),
    urls: bool = typer.Option(False, "--urls"),
) -> None:
    """Alias for [cyan]wboxcli log[/cyan] — latest apply-run table or per-job log tail."""
    _run_log(job_id=job_id, tail=tail, show_urls=urls)


@app.command("doctor")
def doctor_cmd(
    wbox_smoke: bool = typer.Option(
        False,
        "--wbox-smoke",
        help="Load Whitebox login page with Playwright (needs JOBCLI_USERNAME / JOBCLI_PASSWORD)",
    ),
) -> None:
    """Validate Playwright, SQLite, config, and resume JSON."""
    from jobcli.cli.doctor import run_doctor

    raise typer.Exit(run_doctor(console, wbox_smoke=wbox_smoke))


@app.command("analytics-backfill")
def analytics_backfill(
    since_hours: int = typer.Option(
        24,
        "--since-hours",
        help="Include jobs updated in the last N hours (with application logs); default 24",
    ),
    reconcile_logs: bool = typer.Option(
        True,
        "--reconcile/--no-reconcile",
        help="Mark jobs SUBMITTED when application.jsonl shows success",
    ),
    skip_e2e: bool = typer.Option(
        True,
        "--skip-e2e/--include-e2e",
        help="Exclude the greenhouse example/999 E2E test job",
    ),
    clear_queue: bool = typer.Option(
        False,
        "--clear-queue",
        help="Delete unsent local analytics queue rows before uploading (avoids stale E2E payloads)",
    ),
) -> None:
    """Upload apply analytics from local DB + logs (after apply runs that did not sync)."""
    console.print("[bold cyan]WboxCLI Analytics Backfill[/bold cyan]\n")
    config = get_config()
    if not config.tracking_enabled:
        console.print("[yellow]tracking_enabled is false — enable it in config first.[/yellow]")
        raise typer.Exit(1)
    if not (config.job_board_username or "").strip():
        console.print("[red]Run [cyan]wboxcli login[/cyan] first.[/red]")
        raise typer.Exit(1)

    db = get_database()
    from jobcli.analytics.backfill import backfill_apply_analytics

    try:
        result = backfill_apply_analytics(
            db,
            config,
            since_hours=since_hours,
            reconcile_logs=reconcile_logs,
            skip_e2e=skip_e2e,
            clear_local_queue=clear_queue,
        )
    except Exception as exc:
        console.print(f"[red]Backfill failed: {exc}[/red]")
        raise typer.Exit(1)

    console.print(f"  User: [cyan]{result.get('user_id')}[/cyan]")
    console.print(f"  Jobs in snapshot: [bold]{result.get('jobs_attempted', 0)}[/bold]")
    console.print(f"  Submitted: [green]{result.get('jobs_submitted', 0)}[/green]")
    console.print(f"  Failed/other: [red]{result.get('jobs_failed', 0)}[/red]")
    if result.get("jobs_skipped"):
        console.print(f"  Skipped: [yellow]{result.get('jobs_skipped', 0)}[/yellow]")
    flush = result.get("flush") or {}
    if flush.get("status") == "success":
        console.print(f"  [green]✓ Uploaded {flush.get('count', 0)} event(s) to {config.sync_server_url}[/green]")
    else:
        console.print(f"  [yellow]Upload: {flush}[/yellow]")
        console.print("  Run [cyan]wboxcli sync[/cyan] or check sync_server_url / login.")
    console.print(
        "\n[dim]Note: the dashboard shows [bold]last apply run[/bold] job counts per user "
        "(not lifetime totals).[/dim]"
    )


@app.command("sync")
def sync_cmd(
    no_backfill: bool = typer.Option(
        False,
        "--no-backfill",
        help="Sync knowledge/activity only; skip 24h apply analytics backfill",
    ),
    since_hours: int = typer.Option(
        24,
        "--since-hours",
        help="Include jobs updated in the last N hours for analytics backfill",
    ),
) -> None:
    """Sync learned knowledge and job activity with the central server."""
    from jobcli.cli.upload_pipeline import (
        DEFAULT_BACKFILL_HOURS,
        print_backfill_step_results,
        print_sync_step_results,
        run_post_apply_upload,
    )

    started_at = time.time()
    db = get_database()
    cfg = get_config()
    hours = since_hours if since_hours > 0 else DEFAULT_BACKFILL_HOURS

    console.print("[bold cyan]JobCLI Synchronization[/bold cyan]")
    console.print("Syncing knowledge and application activity with central server...\n")

    try:
        upload_result = run_post_apply_upload(
            db,
            cfg,
            apply_context=None,
            run_backfill=not no_backfill,
            since_hours=hours,
            console=console,
        )
        results = upload_result.sync or {}
        if upload_result.sync_error and not results:
            console.print(f"[red]Sync error: {upload_result.sync_error}[/red]")
            _track_command_event(db=db, config=cfg, command="sync", started_at=started_at, result="error")
            raise typer.Exit(1)

        if results.get("status") == "success":
            print_sync_step_results(results, console=console)
            console.print("\n[bold green]✓ Synchronization complete[/bold green]")
            if upload_result.backfill and not upload_result.backfill_error:
                print_backfill_step_results(upload_result.backfill, console=console)
            elif not no_backfill and upload_result.backfill_error:
                console.print(
                    f"  [yellow]⚠[/yellow] Analytics backfill failed: {upload_result.backfill_error}"
                )
            _track_command_event(
                db=db,
                config=cfg,
                command="sync",
                started_at=started_at,
                result="success",
                metadata={
                    "activity_count": results.get("activity_count", 0),
                    "usage_event_count": results.get("usage_event_count", 0),
                    "backfill_jobs": (upload_result.backfill or {}).get("jobs_attempted", 0),
                },
            )
        else:
            console.print(f"[red]Sync failed: {results.get('error', 'unknown')}[/red]")
            _track_command_event(db=db, config=cfg, command="sync", started_at=started_at, result="error")
            raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]Sync error: {e}[/red]")
        _track_command_event(db=db, config=cfg, command="sync", started_at=started_at, result="error")
        raise typer.Exit(1)



@app.command("send-daily-report")
def send_daily_report_cmd(
    mock: bool = typer.Option(False, "--mock", help="Send mock report for testing"),
) -> None:
    """Alias for 'wboxcli db send-daily-report'. Fetch today's ATS stats and email them."""
    import os
    from jobcli.analytics.report_mailer import send_daily_report
    server_url = os.getenv("API_URL", "https://whitebox-learning.com/api")
    send_daily_report(server_url, mock=mock)


@app.command("schedule-report")
def schedule_report_cmd(
    owner_email: str = typer.Option(None, "--email", help="The recipient admin email address (defaults to ADMIN_EMAIL in .env)"),
    time: str = typer.Option("18:00", "--time", help="Time to send the report in HH:MM format (24-hour clock)"),
) -> None:
    """Setup a Windows Scheduled Task to run the daily report locally."""
    import subprocess
    from dotenv import load_dotenv
    
    load_dotenv()
    
    if not owner_email:
        owner_email = os.environ.get("ADMIN_EMAIL")
        
    if not owner_email:
        console.print("[red]No admin email provided. Set ADMIN_EMAIL in .env or pass --email.[/red]")
        raise typer.Exit(1)
    
    console.print("[bold cyan]WboxCLI Report Scheduler[/bold cyan]\n")
    
    # Resolve the wboxcli executable path robustly.
    # Prefer the venv-installed shim; fall back to the venv python -m jobcli.
    import shutil
    exe_path = shutil.which("wboxcli")
    if exe_path:
        command = f'"{exe_path}" db send-daily-report'
    else:
        # Use the current interpreter so the venv is guaranteed correct.
        command = f'"{sys.executable}" -m jobcli db send-daily-report'

    # Create the schtasks command
    task_name = "WboxCLI_Daily_Report"
    schtasks_cmd = [
        "schtasks", "/Create", "/SC", "DAILY", "/TN", task_name,
        "/TR", command, "/ST", time, "/F"
    ]

    try:
        subprocess.run(schtasks_cmd, capture_output=True, text=True, check=True)
        console.print(f"[green]Successfully scheduled daily report at {time} daily.[/green]")
        console.print(f"[dim]Scheduled command: {command}[/dim]")
    except subprocess.CalledProcessError as e:
        console.print("[red]Failed to schedule task. Try running as Administrator.[/red]")
        console.print(f"[red]Error: {e.stderr.strip()}[/red]")
        raise typer.Exit(1)

@app.command()
def server() -> None:
    """Start the JobCLI web UI server."""
    import uvicorn
    def _env_bool(key: str, default: bool) -> bool:
        raw = os.getenv(key)
        if raw is None:
            return default
        return raw.strip().lower() in {"1", "true", "yes", "y", "on"}

    host = os.getenv("JOBCLI_API_HOST", "0.0.0.0")
    port = int(os.getenv("JOBCLI_API_PORT", "8000"))
    reload = _env_bool("JOBCLI_API_RELOAD", True)

    console.print("[bold cyan]Starting JobCLI Web UI...[/bold cyan]")
    dashboard_host = "localhost" if host in {"0.0.0.0", "::"} else host
    console.print(f"Access the dashboard at: [green]http://{dashboard_host}:{port}[/green]")
    uvicorn.run("jobcli.api.main:app", host=host, port=port, reload=reload)

@app.command()
def agent(
    prompt: str = typer.Argument(..., help="The task you want the agent to perform"),
    max_steps: int = typer.Option(12, help="Maximum number of steps the agent can take"),
) -> None:
    """Launch the autonomous coding agent."""
    from jobcli.agents.coder.agent import CodingAgent
    
    console.print(f"[bold cyan]JobCLI Autonomous Coder[/bold cyan]\n")
    
    config = get_config()
    ensure_configured(config, require_job_board=False)
    
    if not config.default_llm_provider:
        console.print("[red]No default LLM provider configured. Run [cyan]login[/cyan] to set one.[/red]")
        raise typer.Exit(1)
        
    try:
        agent = CodingAgent(config)
        agent.run(prompt, max_steps=max_steps)
    except KeyboardInterrupt:
        console.print("\n[yellow]Agent stopped by user.[/yellow]")
    except Exception as e:
        console.print(f"\n[red]Fatal Error: {e}[/red]")


@app.command("reset")
def cli_reset(
    force: bool = typer.Option(False, "--force", "-f", help="Reset without confirmation"),
    keep_credentials: bool = typer.Option(
        False,
        "--keep-credentials",
        help="Keep Whitebox email/password and API URL",
    ),
    keep_extension: bool = typer.Option(
        False,
        "--keep-extension",
        help="Keep saved extension path",
    ),
) -> None:
    """Clear Whitebox login, LLM API keys, and resume. Jobs are kept. Full wipe: ``wboxcli db reset``."""
    _run_reset(
        force,
        keep_credentials=keep_credentials,
        keep_extension=keep_extension,
    )


if __name__ == "__main__":
    app()
