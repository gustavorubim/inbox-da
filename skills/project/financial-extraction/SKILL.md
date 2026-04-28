---
name: financial-extraction
description: Extract normalized financial fields from emails, invoices, statements, receipts, remittance advice, bank notices, and attachment summaries.
license: MIT
---

# Financial Extraction Skill

Use this skill when an email or attachment contains financial information that must be written to `structured_extraction`.

Required output fields:
- `document_type`
- `counterparty`
- `account_or_customer`
- `period_start`
- `period_end`
- `currency`
- `amount`
- `due_date`
- `reference_number`
- `financial_category`
- `line_items`
- `evidence`
- `confidence`
- `verification_status`

Rules:
- Use `USD` when the source says dollars or `$`.
- Prefer invoice/reference/remittance numbers over generic message ids.
- Use the final payable/receivable/balance amount, not every amount mentioned.
- Never invent evidence. Every important field needs a source quote.
- If amount, date, or reference evidence conflicts, return the best candidate but make the uncertainty explicit.
