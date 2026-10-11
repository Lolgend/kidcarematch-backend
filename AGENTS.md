## Agent skills

### Issue tracker

GitHub Issues via `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Canonical 5-role vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context (`GLOSSARY.md` + `docs/adr/`). See `docs/agents/domain.md`.

### File Searches

Use `git ls-files` rather than recursive file searches.

### Toolchain & Commands
- Package manager: `uv`
- Run tests: `uv run pytest`
- Type check: `uv run mypy src tests`
- Lint / format: `ruff check .`

### Pull Request (PR)

Use `pr` to form PR body before marking it ready for review.
