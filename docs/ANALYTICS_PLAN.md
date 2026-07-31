# Release and Usage Analytics Implementation Plan

This document is a checklist-driven plan to release `jobcli` (CLI command: `wboxcli`) as a product and to add *usage analytics* that measure how people use the CLI, while keeping user privacy and PII protections intact.

## 0. What exists in this codebase today (verified)

### 0.1 Git repo and main branch
- The repository exists under `project-ashby-automation/` and has a `main` branch reference (`refs/heads/main`).
- There is no local `.github/` directory in this checkout (no GitHub Actions workflows are present here), so CI/release automation must be added.

### 0.2 Existing telemetry vs. usage analytics
- The codebase already implements structured *execution telemetry* in:
  - `src/jobcli/execution/telemetry.py`
- The `TelemetryTracker` emits structured events like:
  - `action_started`, `field_fill_succeeded`, `selector_not_found`, etc.
- That telemetry is currently used locally for debugging/analysis and tests; it is not wired into a network “usage analytics” pipeline.

### 0.3 Central dashboard sync exists (activity + knowledge)
- There is a central sync mechanism in `src/jobcli/sync/` that already POSTs:
  - job activity logs to a central server endpoint (`/job_activity_logs/bulk`)
  - aggregated knowledge updates (`/sync_cli/knowledge_sync`)
- The knowledge exporter explicitly avoids sending PII (see `src/jobcli/sync/extractor.py` and `src/jobcli/sync/constants.py`).

### 0.4 CLI entrypoint is Typer-based
- CLI dispatch is in `src/jobcli/cli/main.py` (Typer).
- Commands include `login`, `setup`, `discover`, `apply`, `sync`, etc.
- There is no existing analytics layer in the CLI entrypoint.

## 1. Release definition (what “done” means)

To treat this as a “product release”, the following must be true:
1. **Code quality gates**: unit/integration tests pass; linting/static checks are clean.
2. **Versioning**: `pyproject.toml` version matches the release tag; changelog/release notes exist.
3. **Artifacts**: build process produces:
   - Python distributable (wheel + sdist)
   - any packaged UI/extension artifacts required by installation
4. **Installation/update reliability**: install scripts fetch the released artifact, not “latest main”.
5. **Documentation**: `README` + docs include:
   - install/run instructions
   - configuration
   - telemetry/analytics policy
6. **Privacy compliance**: analytics is opt-out or opt-in (choose one policy), and no PII is sent.
7. **Observability**: after release, you can monitor errors and (separately) analytics metrics.

## 2. Release implementation plan (end-to-end)

### 2.1 Git branching, main branch, and release tags
1. Ensure `main` is the release branch (protect it).
2. Establish a merge rule:
   - feature branches -> `dev` -> PR -> merge into `main`
3. Require release PR checklist (see Section 6).
4. Create a release tag:
   - `vX.Y.Z`
5. Ensure install scripts can pull the tagged release version.

### 2.2 Versioning and changelog
1. Update `project` version in `pyproject.toml` (`version = "..."`).
2. Add/maintain `CHANGELOG.md` (create if missing).
3. Add release notes (GitHub Release body) with:
   - what changed
   - any migration steps
   - telemetry/analytics changes

### 2.3 Build and packaging
1. Build Python artifacts:
   - `python -m build` (wheel + sdist)
2. Verify that the built distribution contains:
   - `jobcli` package
   - CLI entrypoint `wboxcli`
3. Build any UI/extension assets that must ship.
4. Create a reproducible build record:
   - store build metadata (Python version, lockfile hashes, artifact checksums).

### 2.4 CI/CD (GitHub Actions) - add missing workflows
Because `.github/` is absent locally, add at least:
1. `ci.yml`
   - checkout
   - set up Python (3.10+)
   - install deps
   - run:
     - `pytest`
     - `ruff` (lint)
     - `mypy` (type check)
     - `black --check` (or equivalent formatting check)
2. `build.yml`
   - builds artifacts on tag push
3. `release.yml`
   - publishes to PyPI (if desired) and creates GitHub Release artifacts

Security scanning recommended:
- SCA (dependency scan)
- optional code scanning

### 2.5 Release script updates (install/update)
Currently, install scripts fetch from `main`/`dev` branches (raw GitHub).
To make release robust:
1. Add versioned install entrypoints:
   - `.../releases/download/vX.Y.Z/...`
2. Update:
   - `scripts/install.sh`
   - `scripts/install.ps1`
3. Add signature/checksum verification (minimum: SHA256).

## 3. Usage analytics requirement

You asked for “analytics of usage too”. In this product, usage analytics should measure:
1. **Adoption**: how many installs/runs happen (aggregated, anonymized)
2. **Command usage**: which commands are used (`login`, `setup`, `discover`, `apply`, `sync`, etc.)
3. **Feature usage**: optional features such as TUI server/dashboard, legacy UI mode, headless mode flags
4. **Success/failure**: command exit status and failure category (no PII)
5. **Funnel**: where users stop (e.g., `login` done but never `apply`)
6. **Performance**: coarse durations (e.g., total runtime per command, network timeouts)

## 4. Privacy and compliance requirements for analytics

Non-negotiable rules:
1. **No PII in analytics payload**
   - Do not send email, phone, resume text, API keys, full job URLs, etc.
2. **No PII-derived correlation**
   - Use anonymous IDs that are resettable.
3. **User consent**
   - Choose a policy and document it:
     - **Opt-in**: analytics disabled by default until user enables
     - **Opt-out**: analytics enabled by default with a prominent disable toggle
4. **Local buffering**
   - Queue events locally when offline and retry with backoff.
5. **Data minimization**
   - Send aggregated counters where possible.

This codebase already practices PII minimization for knowledge sync:
- `src/jobcli/sync/extractor.py` skips personal labels.
Usage analytics must apply the same mindset: minimize and sanitize aggressively.

## 5. Proposed analytics architecture (matches existing code style)

### 5.1 Components to add
1. `src/jobcli/analytics/usage.py`
   - Defines:
     - event schema (Pydantic model)
     - event builder helpers
     - sanitization rules
2. `src/jobcli/analytics/client.py`
   - Network sender (HTTP)
   - retry/backoff
   - batching
3. `src/jobcli/analytics/storage.py`
   - Local queue storage
   - can reuse SQLite (preferred) or a small JSONL file
4. CLI hook:
   - Wrap Typer command lifecycle in `src/jobcli/cli/main.py`
   - Record:
     - command name
     - start time / end time
     - exit status (success/failure)
     - error category (sanitized)
5. Optional: separate “apply run analytics”
   - For `apply`:
     - count number of jobs processed (from local DB)
     - count success/failure by `ApplicationStatus`
     - do NOT send job URLs/titles unless you have explicit consent and a documented policy.

### 5.2 Event schema (example)
Create an analytics “usage_event” with fields like:
- `event_name`: e.g. `cli_command_completed`
- `timestamp`: UTC ISO string
- `command`: one of `{login, setup, discover, apply, sync, resume-upload, ...}`
- `result`: `{success, error}`
- `error_type`: coarse category (e.g. `network_error`, `auth_failed`, `validation_error`)
- `duration_ms`: integer
- `version`: `jobcli` version (from `pyproject.toml`)
- `platform`: OS (`win32`, `darwin`, `linux`)
- `analytics_anonymous_id`: random UUID stored locally
- optional aggregated fields:
  - `jobs_attempted_count`
  - `jobs_succeeded_count`
  - `jobs_failed_count`
- `consent_state`: `{enabled, disabled}`

### 5.3 Configuration (where to store consent)
Add fields to `src/jobcli/profile/schemas.py` `Config`:
- `analytics_opt_in: bool` (or `analytics_enabled: bool`)
- `analytics_anonymous_id: str` (generate once, stored in config or dedicated table)
- `analytics_endpoint_url: Optional[str]`
  - defaults to the central server base (can be derived from `sync_server_url`)

Add CLI commands:
- `wboxcli analytics status`
- `wboxcli analytics enable`
- `wboxcli analytics disable`

### 5.4 Transport and batching
Network rules:
1. Batch events locally (e.g. 20 events or 10 MB threshold).
2. Send on:
   - graceful exit (best effort)
   - periodic background timer (optional)
   - before sync (optional)
3. Use exponential backoff:
   - base delay (e.g. 1s) -> max (e.g. 1h)
4. Hard limit:
   - keep queue <= N events; drop oldest if necessary.

### 5.5 Server-side contract
Define a new central endpoint (or extend an existing one):
- `POST /analytics/usage_events/bulk`

Server expectations:
- Validate schema
- Reject payloads with PII patterns (belt-and-suspenders)
- Store aggregated results (preferred) or raw events with strict retention
- Provide dashboards (Section 7)

## 6. PR/release gate checklist (what “perfect” means)

This is the release checklist you should require on the final release PR:
1. **Working tree is clean** (no unintended changes).
2. **Tests pass**:
   - `pytest tests/`
   - run relevant integration tests (ATS flows) in CI if feasible.
3. **Linters/type checks pass**:
   - `ruff`
   - `mypy`
   - `black --check`
4. **Build succeeds**:
   - `python -m build`
5. **No secrets**:
   - verify no credentials are committed (search `.env`, tokens).
6. **Analytics privacy**:
   - analytics payload does not contain PII or job URLs
   - consent default documented
   - opt-out command works
7. **Docs updated**:
   - README includes analytics policy and how to opt out
   - a new analytics doc is linked from README
8. **Release artifacts available**:
   - checksums computed
   - install script points to versioned artifacts

## 7. Dashboards and monitoring

Create at least these usage analytics dashboards:
1. Adoption:
   - unique anonymous IDs per day/week
   - install->first-command funnel
2. Command usage:
   - counts per command
   - median duration per command
3. Reliability:
   - error rates by `error_type`
   - top error categories by volume
4. Activation funnel:
   - login success rate -> setup success -> discover -> apply -> sync
5. Privacy metrics:
   - verify PII rejection rate is near zero
   - verify opt-out adoption

Alerting:
- spikes in auth failures
- spikes in network timeouts
- spikes in client schema validation failures

## 8. Implementation steps (ordered)

### Step 1: Create docs + contracts
1. Create `docs/USAGE_ANALYTICS.md` describing:
   - what is collected
   - why it is collected
   - opt-in/opt-out instructions
2. Create `docs/ANALYTICS_DATA_CONTRACT.md` with example payloads.

### Step 2: Implement client + local queue
1. Add `src/jobcli/analytics/usage.py` (event models + sanitization).
2. Add `src/jobcli/analytics/storage.py` (queue persistence).
3. Add `src/jobcli/analytics/client.py` (batch send, retry, backoff).

### Step 3: Add consent config + CLI controls
1. Extend `Config` in `src/jobcli/profile/schemas.py`.
2. Add `wboxcli analytics ...` subcommands.
3. Ensure default consent is defined and documented.

### Step 4: Instrument CLI commands
1. Add Typer callback/wrapper in `src/jobcli/cli/main.py`:
   - capture `command_name`
   - capture start/end and exit status
2. For `apply`, record aggregated job counts and outcomes:
   - attempted jobs
   - succeeded/failed counts
   - do not send job URLs/titles.

### Step 5: Add server endpoint (outside this repo)
1. Implement `/analytics/usage_events/bulk`
2. Implement PII validation and retention policies.

### Step 6: Add tests
1. Unit tests for:
   - consent logic
   - sanitization (no PII fields)
   - batching behavior
2. Integration tests for:
   - HTTP client success/failure + retry

### Step 7: Release
1. Merge to `main`
2. Tag `vX.Y.Z`
3. Publish artifacts
4. Verify installation/update works for at least:
   - Windows PowerShell
   - one macOS/Linux shell

## 9. Open questions (must be answered before building)

1. Consent policy:
   - opt-in or opt-out by default?
2. Analytics endpoint ownership:
   - is the central sync server where this should go, or a separate service?
3. What identifiers are allowed:
   - only random anonymous UUID?
4. Which command payload fields are acceptable:
   - can we include command flags (e.g. `--legacy-ui`), or keep it minimal?

