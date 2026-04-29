# Inbox DA Agent Memory

This project extracts financial information from Oracle-backed inbox messages into SQLite.

Durable rules:
- `gemini-3-flash-preview` is the required live model for extraction, verification, image parsing, and scanned or low-text PDF parsing.
- The agent must never receive unrestricted SQL write access. Database mutations go through typed repository functions.
- The final persisted schema is `structured_extraction`; model outputs may be normalized before validation.
- Evidence is required for important fields. Prefer exact quotes from the email body, parsed attachments, or vision summaries.
- When extractor outputs disagree, preserve an auditable row with `verification_status='needs_review'`.
- Do not read or save credentials. Never include `.env` content in model prompts or memory.
- Use subagents for attachment analysis, financial field extraction, evidence verification, and conflict review when live DeepAgents are enabled.
