---
name: verification
description: Verify two financial extraction candidates against source evidence and decide whether the result is verified or needs review.
license: MIT
---

# Verification Skill

Use this skill after two extraction passes have produced candidates.

Return `verified` only when:
- document type matches the source,
- counterparty matches the source,
- amount and currency match within one cent,
- due date and reference number are either matched or absent from the source,
- evidence contains enough source quotes to audit the row.

Return `needs_review` when:
- extractor candidates disagree on amount, currency, counterparty, due date, or reference number,
- evidence is missing or vague,
- an attachment total conflicts with the email body,
- the document appears to be a notice or statement rather than an invoice/payable.
