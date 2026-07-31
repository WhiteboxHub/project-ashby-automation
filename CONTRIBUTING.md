# Contributing to WboxCLI

Thanks for your interest in improving WboxCLI! This document explains how to set up
a dev environment, what conventions we follow, and how to get a change merged.

## Quick start

```bash
git clone https://github.com/WhiteboxHub/project-ashby-automation.git
cd project-ashby-automation

python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate

pip install --upgrade pip
pip install -e ".[dev]"
playwright install chromium

cp .env.example .env                 # optional, for local scripts only
```

Run the CLI from source without installing globally:

```bash
PYTHONPATH=src python -m jobcli.cli.entry         # interactive TUI
PYTHONPATH=src python -m jobcli.cli.main --help   # direct CLI
```

## Branching model

| Branch | Purpose |
|---|---|
| `main` | Stable, released code. Protected. PRs only. |
| `dev` | Integration branch for the next release. PRs from feature branches land here. |
| `feat/<short-name>` | Single feature or fix; branch from `dev`. |

Open PRs against `dev` unless you're shipping a hotfix.

## Commit messages

Use short, imperative subject lines. Conventional Commits prefixes are encouraged
but not required:

```
feat(apply): skip linkedin URLs with 600s manual timeout
fix(memory): never overwrite human-entered answers
docs(readme): correct install one-liner for Windows
chore(deps): bump playwright to 1.45
```

## Code style

- **Python**: formatted with `black` (line length 100), linted with `ruff`,
  type-checked with `mypy`. Run before pushing:

  ```bash
  black src tests
  ruff check src tests
  mypy src
  ```

- Public functions and classes need type hints and a one-line docstring.
- Don't add narrating comments (`# increment counter`). Comments should explain
  *why*, not *what*.

## Tests

```bash
pytest                               # full suite
pytest tests/test_memory_system.py   # single file
pytest -k "confidence"               # by keyword
```

A few tests reference legacy `jobcli.core.*` modules that were refactored away;
those are tracked in [issue tracker] and not required to pass for PRs that don't
touch the affected modules.

## Reporting issues

Use the GitHub issue templates:

- **Bug report** — include OS, Python version, `wboxcli --version`, the failing
  command, the full traceback, and steps to reproduce.
- **Feature request** — describe the problem first, then propose the solution.

## Security issues

**Please do not file public issues for security problems.** See [SECURITY.md](SECURITY.md)
for the responsible-disclosure process.

## Code of conduct

By participating you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
