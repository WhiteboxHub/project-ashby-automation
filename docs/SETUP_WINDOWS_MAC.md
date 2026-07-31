# JobCLI + TalentScreen Extension — Setup Guide (Windows & macOS)

Complete command reference for local development: build the Chrome extension, copy it into the CLI repo, run onboarding, and apply to jobs.

---

## What you need

| Requirement | Notes |
|-------------|--------|
| **Python 3.10+** | `python --version` |
| **Git** | To clone repos |
| **Two folders** (sibling repos under `wbox/`) | `project-talentscreen-autofill-extension` + `project-ashby-automation` |

Optional: global install via `install.sh` / `install.ps1` (see main [README](../README.md)).

---

## How the extension reaches the browser

The browser **never** loads the `.zip` file directly.

```
extension/talentscreen-autofill.zip     (you copy the built ZIP here)
        ↓  automatic unzip (Python zipfile)
~/.jobcli/extension_unpacked/         (Chrome loads THIS folder)
        ↓  Playwright flags
--load-extension=.../extension_unpacked
```

Unzip runs when you run `doctor`, `setup`, or `apply` — if `extension/talentscreen-autofill.zip` exists and is missing or newer than the unpacked folder.

On **macOS / Linux**, `./build.sh` in the CLI repo also unpacks the ZIP during the dev build. **`build.bat` on Windows does not unpack** — copy the ZIP first, then run `doctor` or `apply` (or `./build.sh` from Git Bash in the CLI repo).

| Path | Role |
|------|------|
| `project-ashby-automation/extension/talentscreen-autofill.zip` | Transport artifact (gitignored) |
| `~/.jobcli/extension_unpacked/` | What Chrome actually loads |
| `~/.jobcli/jobcli.db` | Login, API keys, resume paths, jobs queue |

---

## Part 1 — Build the Chrome extension

### macOS / Linux (Git Bash or Terminal)

```bash
cd /path/to/wbox/project-talentscreen-autofill-extension
./build.sh
```

Output: `dist/talentscreen-autofill-v2.0.0.zip` (version from `manifest.json`).

Copy into the CLI repo:

```bash
cp dist/talentscreen-autofill-v*.zip ../project-ashby-automation/extension/talentscreen-autofill.zip
```

Or from the extension repo:

```bash
./scripts/copy-to-cli.sh
```

### Windows

The ZIP is created under **`dist\`** (not the project root):

```text
project-talentscreen-autofill-extension\dist\talentscreen-autofill-v2.0.0.zip
```

**Option A — PowerShell (recommended)**

Run in **PowerShell**, not CMD (`.\build.ps1` does not run correctly from CMD alone):

```powershell
cd C:\Users\sampa\OneDrive\Desktop\wbox\project-talentscreen-autofill-extension
.\build.ps1
```

You should see **`SUCCESS`** and the full path to the ZIP. Open the folder:

```powershell
explorer .\dist
```

Copy into the CLI repo (wildcard-safe):

```powershell
$zip = Get-ChildItem dist\talentscreen-autofill-v*.zip | Select-Object -First 1
Copy-Item $zip.FullName "..\project-ashby-automation\extension\talentscreen-autofill.zip" -Force
```

**From CMD** (if you only have Command Prompt):

```cmd
cd C:\Users\sampa\OneDrive\Desktop\wbox\project-talentscreen-autofill-extension
powershell -NoProfile -ExecutionPolicy Bypass -File build.ps1
dir dist\*.zip
```

**Option B — Git Bash**

```bash
cd /c/Users/sampa/OneDrive/Desktop/wbox/project-talentscreen-autofill-extension
./build.sh
```

If `zip` is not installed, `build.sh` automatically runs `build.ps1` via PowerShell.

```bash
cp dist/talentscreen-autofill-v*.zip ../project-ashby-automation/extension/talentscreen-autofill.zip
```

---

## Part 2 — Set up the CLI (dev tree)

### macOS / Linux

```bash
cd /path/to/wbox/project-ashby-automation
./build.sh
source .venv/bin/activate
```

Force reinstall extension after a new ZIP:

```bash
FORCE_REINSTALL_EXTENSION=1 ./build.sh
```

### Windows — Command Prompt (CMD)

```cmd
cd C:\Users\sampa\OneDrive\Desktop\wbox\project-ashby-automation
build.bat
```

`build.bat` activates `.venv`, runs `playwright install chromium`, sets `PYTHONPATH=src`, and opens the **interactive TUI**.

**Do not** use `$env:PYTHONPATH` in CMD — that is PowerShell only. In CMD use:

```cmd
set PYTHONPATH=src
```

### Windows — PowerShell

```powershell
cd C:\Users\sampa\OneDrive\Desktop\wbox\project-ashby-automation
.\build.bat
```

Or run commands manually:

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m jobcli.cli.main doctor
```

---

## Part 3 — First-time user vs returning user

### Where settings are stored

Everything is saved in:

| OS | Database path |
|----|----------------|
| Windows | `C:\Users\<you>\.jobcli\jobcli.db` |
| macOS / Linux | `~/.jobcli/jobcli.db` |

Stored data includes: Whitebox email/password, LLM API key, resume PDF/JSON paths, parsed resume, discovered jobs.

### First-time user (empty database)

**Recommended:** interactive onboarding.

| OS | Command |
|----|---------|
| Windows | `build.bat` then follow prompts |
| macOS / Linux | `./build.sh` then `wboxcli` or `source .venv/bin/activate && wboxcli` |

Onboarding order:

1. Whitebox Learning email + password (browser + extension smoke test)
2. LLM provider + API key (OpenAI / Anthropic / Gemini)
3. Resume PDF path + Resume JSON path
4. `discover` (pull jobs into local DB)

Then run:

```text
apply --limit 1
```

inside the TUI, or use CLI commands below.

### Returning user (database already exists)

`apply` **does not** ask for login again — it reads SQLite.

You will see the welcome banner and `▶ Next step: apply` immediately.

### Force onboarding again (simulate new user)

**Windows CMD:**

```cmd
del "%USERPROFILE%\.jobcli\jobcli.db"
build.bat
```

**macOS / Linux:**

```bash
rm -f ~/.jobcli/jobcli.db
./build.sh
# then wboxcli
```

Or (with `PYTHONPATH=src` set):

```cmd
.\.venv\Scripts\python.exe -m jobcli.cli.main db reset --force
build.bat
```

---

## Part 4 — Command reference

### Run from dev repo (no global install)

Set Python path every session:

| Shell | Set `PYTHONPATH` |
|-------|------------------|
| **CMD** | `set PYTHONPATH=src` |
| **PowerShell** | `$env:PYTHONPATH = "src"` |
| **bash / Git Bash** | `export PYTHONPATH=src` |

Use the project venv Python:

| OS | Python executable |
|----|-------------------|
| Windows | `.\.venv\Scripts\python.exe` |
| macOS / Linux | `.venv/bin/python` |

Generic form:

```text
<python> -m jobcli.cli.main <command> [options]
```

### Health & setup

| Command | What it does |
|---------|----------------|
| `doctor` | Python, Playwright, DB, LLM key, extension ZIP + unpacked folder |
| `setup` | Validate config, extension, browser smoke test |
| `login` | Set / update Whitebox credentials |
| `resume-upload --pdf PATH --json PATH` | Load resume into DB (+ extension storage on apply) |
| `discover` | Fetch jobs from Whitebox API into local DB |
| `config` | View or edit saved settings |

**Windows CMD examples:**

```cmd
cd C:\Users\sampa\OneDrive\Desktop\wbox\project-ashby-automation
set PYTHONPATH=src
.\.venv\Scripts\python.exe -m jobcli.cli.main doctor
.\.venv\Scripts\python.exe -m jobcli.cli.main login
.\.venv\Scripts\python.exe -m jobcli.cli.main resume-upload --pdf "C:\path\resume.pdf" --json "C:\path\resume.json"
.\.venv\Scripts\python.exe -m jobcli.cli.main discover
.\.venv\Scripts\python.exe -m jobcli.cli.main apply --limit 1
```

**macOS / Linux examples:**

```bash
cd /path/to/project-ashby-automation
export PYTHONPATH=src
.venv/bin/python -m jobcli.cli.main doctor
.venv/bin/python -m jobcli.cli.main apply --limit 1
```

### Apply to jobs

| Command | What it does |
|---------|----------------|
| `apply` | Apply to **all** pending jobs (Chrome visible) |
| `apply --limit 1` | Apply to **one** job (best for testing) |
| `apply --limit 5` | Apply to five jobs |
| `apply --url "https://..."` | Single URL |
| `apply --mode supervised` | Default; pauses for review / handoff |
| `apply --mode auto` | More autonomous |
| `apply --mode manual` | More human checkpoints |

### Interactive TUI (`build.bat` / `wboxcli`)

Inside the menu, type **short commands** (no `jobcli` prefix):

| TUI command | Same as |
|-------------|---------|
| `apply` | `jobcli apply` |
| `apply --limit 1` | one job |
| `discover` | `jobcli discover` |
| `doctor` | `jobcli doctor` |
| `setup` | re-run onboarding |
| `login` | credentials |
| `status` | config + pending job count |
| `jobs` | list pending jobs |
| `help` | command list |
| `exit` | quit (does **not** delete DB) |

`jobcli apply` also works in the TUI (prefix is stripped).

### Extension refresh after rebuild

Copy a new ZIP to `extension/talentscreen-autofill.zip`, then reinstall the unpacked copy:

| OS | Command |
|----|---------|
| **Windows CMD** | `set FORCE_REINSTALL_EXTENSION=1` then `set PYTHONPATH=src` and `.\.venv\Scripts\python.exe -m jobcli.cli.main doctor` |
| **Windows Git Bash** (CLI repo) | `FORCE_REINSTALL_EXTENSION=1 ./build.sh` |
| **macOS / Linux** | `FORCE_REINSTALL_EXTENSION=1 ./build.sh` |

`build.bat` alone does **not** re-unpack the extension; use `doctor` or `apply` after setting `FORCE_REINSTALL_EXTENSION=1`, or run CLI `./build.sh` from Git Bash.

Or delete unpacked folder:

```text
Windows:  rmdir /s /q "%USERPROFILE%\.jobcli\extension_unpacked"
macOS:    rm -rf ~/.jobcli/extension_unpacked
```

Next `doctor` / `apply` unpacks from `extension/talentscreen-autofill.zip` again.

---

## Part 5 — What happens when you `apply`

Fixed pipeline on each job page (strict order):

| Phase | What | Terminal banner |
|-------|------|-------------------|
| **1** | Chrome **extension** autofill | `Chrome extension autofill (1/4)` |
| **2** | **Rules-based** ATS fill (Greenhouse, etc.) | `Rules-based fill (2/4)` |
| **3** | **LLM** agent (upload, dropdowns, custom Q) | `LLM autofill (3/4)` |
| **4** | **Human-in-the-loop** (only if 1–3 leave gaps) | `Human-in-the-loop (4/4)` |

At the start you also see: `Pipeline: ① Extension → ② Rules → ③ LLM → ④ Human (if needed)`

After **resume upload**, phases **1 → 2** run again, then the LLM continues (phase 3).

Phase 4 triggers when required fields are still empty, validation errors remain, or supervised mode needs you in the browser — **never** before extension/rules/LLM on first pass.

### Extension side panel vs CLI

| UI | CLI behavior |
|----|----------------|
| Side panel “Get Started” (upload JSON/PDF) | **Ignored** for `apply` — onboarding UI for manual Chrome use |
| Form on the job page | Filled via storage injection + `fill_form` + LLM |

If the form shows your name/email/resume, autofill **worked** even if the side panel still says “Get Started”.

### Terminal lines to look for

```text
Pipeline: ① Extension → ② Rules → ③ LLM → ④ Human (if needed)
Chrome extension autofill (1/4)
Running TalentScreen extension autofill…
Extension filled N field(s): …   (or: no new visible fields)
Rules-based fill (2/4)
Rules filled N field(s): first_name, last_name, email, …
LLM autofill (3/4)
AI iteration 1/3
Fill summary — extension: N, rules: M, LLM iteration 1: K action(s).
Human-in-the-loop (4/4)    ← only if still incomplete
```

Then `Status: submitted` or you finish in the browser and press ENTER.

---

## Part 6 — Troubleshooting

### `ModuleNotFoundError: No module named 'jobcli.cli'`

You forgot `PYTHONPATH=src` or used system `python` instead of `.venv`.

**Fix (CMD):**

```cmd
set PYTHONPATH=src
.\.venv\Scripts\python.exe -m jobcli.cli.main doctor
```

### `$env:PYTHONPATH` fails in CMD

Use `set PYTHONPATH=src` in CMD, or open **PowerShell** for `$env:PYTHONPATH = "src"`.

### Playwright: `Executable doesn't exist at ... chromium-1217`

Install Chromium for the **same** Python you use to run `apply`:

```cmd
set PYTHONPATH=src
.\.venv\Scripts\python.exe -m playwright install chromium
```

Do **not** mix global `python` and `.venv` for apply.

### `zip: command not found` (Git Bash on Windows)

Re-run `./build.sh` in the extension repo — it should delegate to `build.ps1`. Or run `.\build.ps1` in **PowerShell**, or `powershell -File build.ps1` from CMD.

### Ran `.\build.ps1` but no ZIP file

1. Use **PowerShell** (or `powershell -File build.ps1` from CMD), not CMD-only `.\build.ps1`.
2. Look in **`dist\`**, not the repo root: `dir dist\*.zip`.
3. Run `explorer .\dist` after a successful build.

### Extension not updating after new build

```cmd
set FORCE_REINSTALL_EXTENSION=1
set PYTHONPATH=src
.\.venv\Scripts\python.exe -m jobcli.cli.main doctor
```

Confirm ZIP exists: `extension\talentscreen-autofill.zip`.

### `Sync failed: 422` after successful apply

Post-run sync to Whitebox API failed; **apply/submit still succeeded** locally. Safe to ignore unless you need cloud pattern sync.

### No login prompts on a “new” machine

Delete `~/.jobcli/jobcli.db` (or never ran onboarding on this PC). `apply` alone never runs the wizard.

---

## Part 7 — Quick copy-paste workflows

### Windows — full dev test (CMD)

```cmd
cd C:\Users\sampa\OneDrive\Desktop\wbox\project-talentscreen-autofill-extension
powershell -NoProfile -ExecutionPolicy Bypass -File build.ps1
dir dist\*.zip
copy /Y dist\talentscreen-autofill-v*.zip ..\project-ashby-automation\extension\talentscreen-autofill.zip

cd ..\project-ashby-automation
set PYTHONPATH=src
.\.venv\Scripts\python.exe -m jobcli.cli.main doctor
build.bat
```

In TUI: `apply --limit 1`

Or without TUI (after `doctor` unpacked the extension):

```cmd
cd C:\Users\sampa\OneDrive\Desktop\wbox\project-ashby-automation
set PYTHONPATH=src
.\.venv\Scripts\python.exe -m jobcli.cli.main doctor
.\.venv\Scripts\python.exe -m jobcli.cli.main apply --limit 1
```

### macOS / Linux — full dev test

```bash
cd ../project-talentscreen-autofill-extension
./build.sh
./scripts/copy-to-cli.sh

cd ../project-ashby-automation
./build.sh
source .venv/bin/activate
export PYTHONPATH=src
python -m jobcli.cli.main doctor
python -m jobcli.cli.main apply --limit 1
```

---

## Related docs

- [README.md](../README.md) — features, architecture, global install
- [QUICKSTART.md](QUICKSTART.md) — older API-oriented quickstart (code samples)
