---
name: attachment-analysis
description: Analyze parsed attachments and vision summaries for financial evidence, including PDFs, spreadsheets, text files, and images.
license: MIT
---

# Attachment Analysis Skill

Use this skill when financial evidence appears in attachments or Gemini vision summaries.

Attachment rules:
- TXT bodies are direct evidence.
- CSV/XLSX rows are structured evidence; cite column names when possible.
- Text PDFs are source evidence when extracted text is available.
- Image and low-text PDF summaries are supporting evidence from Gemini vision.
- Keep filename and attachment id with every attachment-derived quote.

When attachment evidence and email body disagree, prefer explicit attachment totals for amount fields and mark the conflict for verification.
