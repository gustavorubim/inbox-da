# Portability Guide

This project has two separable concerns:

1. Ingest messages and attachments into the local SQLite audit store.
2. Extract, verify, review, trace, and report structured financial fields from
   those local rows.

Keep that boundary intact when porting the project. New inbox integrations
should write through typed repository functions in `src/inbox_da/db.py`; agents
should never receive direct SQL write access.

## Porting To Another Inbox

The current production inbox adapter is `src/inbox_da/oracle_inbox.py`. It reads
Oracle rows with `oracledb`, maps configured columns into `OracleEmailRecord`
and `OracleAttachmentRecord`, writes attachment files under
`data/attachments/<email_id>/`, and inserts local rows through `db.insert_email`
and `db.insert_attachment`.

To port to another inbox source, adapt or replace these files:

| File | What to change |
|---|---|
| `src/inbox_da/config.py` | Add source-specific settings and safe credential loading. Prefer environment variables for secrets. |
| `configs/default.yaml` | Add non-secret defaults, table/mailbox names, and column or field mappings. |
| `src/inbox_da/oracle_inbox.py` | Replace with a new adapter that exposes `fetch_emails()` and `fetch_attachments()` style methods, or add a sibling adapter. |
| `src/inbox_da/cli.py` | Add a `sync-<source>` command and decide whether `watch` should poll that source before claiming local emails. |
| `src/inbox_da/db.py` | Add only typed repository helpers needed by the adapter. Avoid raw write SQL outside this layer. |
| `tests/` | Mock the live source client and test duplicate handling, attachment writes, and CLI sync behavior. |
| `README.md` | Update commands, configuration, and operational workflow. |

For Gmail, IMAP, Microsoft Graph, S3, or another database, keep the adapter
output the same shape:

- `message_id`: stable unique id from the source system.
- `sender`: source sender or account label.
- `received_at`: ISO timestamp string.
- `subject`: message subject.
- `body`: plain text body available to extraction.
- attachments: filename, MIME type, and bytes.

The local SQLite schema does not need to change for most inbox ports. If you
need more source metadata, prefer adding a new local table keyed by `email_id`
instead of widening prompts with operational details.

## Porting Extraction Rules

The extraction contract is defined by `src/inbox_da/models.py` and persisted in
`structured_extraction`. The current rules expect financial fields such as
document type, counterparty, amount, currency, due date, line items, evidence,
confidence, and verification status.

To change extraction rules, adapt these files:

| File | What to change |
|---|---|
| `src/inbox_da/models.py` | Update the Pydantic contracts for accepted fields and validation rules. |
| `src/inbox_da/schema.sql` | Add or adjust persisted columns only when the final structured output changes. Preserve append-only review behavior. |
| `src/inbox_da/agent.py` | Update normalization, heuristic offline extraction, verification mapping, and final candidate selection. |
| `skills/project/financial-extraction/SKILL.md` | Teach the extractor subagent the new fields, evidence requirements, and output JSON shape. |
| `skills/project/verification/SKILL.md` | Teach the verifier how to compare candidates for the new rule set. |
| `skills/project/attachment-analysis/SKILL.md` | Update attachment-specific evidence guidance when new document types need it. |
| `src/inbox_da/web.py` and `src/inbox_da/templates/*.html` | Update review forms, detail pages, and field override controls. |
| `tests/` | Add model, normalization, workflow, review, and web coverage for the new rules. |
| `README.md` | Update schema tables, examples, review workflow, and exported output descriptions. |

Important audit rule: review actions must be appended to `review_actions`. If a
review changes the accepted final answer, append a new `structured_extraction`
row instead of updating the old model output row.

## Safe Porting Checklist

1. Add the new inbox or extraction config without secrets.
2. Keep live service calls behind interfaces so tests can mock them.
3. Import source messages into local `emails` and `attachments`.
4. Run an offline processing pass against local rows.
5. Verify `email_extraction_traces`, `task_traces`, and `email_processing_costs`
   are still written.
6. Verify review approvals, candidate selection, field selection, and manual
   overrides append audit rows.
7. Update `README.md` and any operator runbooks.
8. Run:

```bash
uv run ruff format --check .
uv run ruff check .
uv run pytest --cov=src/inbox_da --cov-report=term-missing --cov-fail-under=90
```
