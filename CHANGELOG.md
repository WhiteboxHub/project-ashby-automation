# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `LICENSE` (MIT), `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, `CHANGELOG.md`.
- `.env.example` template; `.env` no longer ships with real values.
- GitHub issue templates and pull-request template under `.github/`.
- `MANIFEST.in` for clean source distributions.
- `Makefile` with common dev targets (`install`, `test`, `lint`, `format`, `clean`).
- `requirements-dev.txt` separated from runtime `requirements.txt`.

### Changed
- All install / uninstall URLs now point to
  `github.com/WhiteboxHub/project-ashby-automation` (was `WhiteboxHub/wbox-cli`).
- Tightened `.gitignore`; stopped tracking `.deepeval/` cache files.

### Known issues
- Several tests under `tests/` import legacy `jobcli.core.*` modules that were
  refactored into `src/jobcli/automation/`, `src/jobcli/orchestration/`, etc.
  These tests are expected to fail until the suite is rewritten.

## [0.1.0] — 2026-05-26

Initial public release.

### Features
- Interactive TUI onboarding (`wboxcli`): WBL login, LLM key, resume paths,
  profile summary, auto `discover`.
- `wboxcli apply` with always-visible browser, four-step fill pipeline
  (Extension → Rules → LLM → Human prompt), and Ctrl+C resume support.
- `wboxcli discover` with source-filtered ingest (trueup.io, hiring.cafe,
  jobright, linkedin) via the WBL job-listings API.
- TalentScreen Chrome extension auto-loaded for autofill.
- Local learning & memory engine with confidence-gated answers and
  merge-protection for human-entered values.
- Anonymous crowd-intelligence sync of high-confidence, non-PII patterns.
- Multi-provider LLM support (OpenAI, Anthropic, Gemini).
- Structured logging with screenshots and DOM snapshots.

[Unreleased]: https://github.com/WhiteboxHub/project-ashby-automation/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/WhiteboxHub/project-ashby-automation/releases/tag/v0.1.0
