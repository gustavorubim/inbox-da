# Repository Agent Instructions

This repo implements a DeepAgent financial inbox monitor. Before making changes,
read `README.md` and inspect the current schema, CLI, and tests.

## Required Workflow

- Use `uv` and the local `.venv`; do not rely on global Python packages.
- Keep the project in `src/` layout under `src/inbox_da`.
- Use Ruff for formatting and linting.
- Maintain at least 90% line coverage.
- Run these gates before finishing:

```bash
uv run ruff format --check .
uv run ruff check .
uv run pytest --cov=src/inbox_da --cov-report=term-missing --cov-fail-under=90
```

## Design Constraints

- The required model default is `gemini-3-flash-preview`.
- Use the shared model factory for extraction, verification, image parsing, and
  scanned or low-text PDF parsing.
- Keep live Gemini calls behind interfaces so tests can mock them.
- Do not give agents unrestricted SQL write access.
- Write database changes through typed repository functions.
- Preserve `emails`, `attachments`, `structured_extraction`,
  `extraction_runs`, `task_traces`, `email_extraction_traces`, and
  `email_processing_costs`, and `review_actions`.
- Keep terminal output useful with Rich and keep task traces auditable in SQLite.
- Keep the FastAPI web monitor operational when changing schema, review, trace,
  extraction, or cost behavior.
- Preserve review auditability: candidate selection, field selection, approval,
  and manual overrides must be appended to `review_actions` rather than
  overwriting raw model outputs. If a review action changes the accepted final
  answer, append a new `structured_extraction` row instead of updating the old
  row in place.
- Keep per-email trace and cost writes in sync with processing behavior.
- Update README whenever commands, configuration, schema, or workflow behavior
  changes.
- Keep DeepAgents sync subagents, skills, and memory wired through the shared
  model provider rather than ad hoc agent construction.
- Async subagents must remain optional because they require a remote Agent
  Protocol or LangGraph server.
- Never place credentials or `.env` contents in skills, memory, traces, or model
  prompts.
