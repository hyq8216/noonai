# Noon Studio engineering instructions

Build a full Saudi noon operations platform with strong automation. Keep the existing
supplier intake, bilingual content, media, catalog campaigns, human approval,
noon readback, orders, warehouse, purchasing, finance and recovery modules connected.
Read `PRODUCT.md`, `docs/AUTOMATION_ROADMAP.md` and the relevant source before editing.

## Environment

- Python 3.12, Node.js 22, Linux cloud development; macOS desktop is a separate target.
- Setup: `bash scripts/setup.sh`; cached-container maintenance: `bash scripts/maintenance.sh`.
- Always call `.venv/bin/python` explicitly; setup-shell exports do not persist.
- Full verification: `bash scripts/check.sh`.
- Targeted tests: `.venv/bin/python -m unittest discover -s workbench/tests -p 'test_automation*.py' -v`.
- Use a temporary `--data` directory for every test/preview. Never open `workbench/data` during tests.
- Linux cannot validate Swift/macOS packaging. Do not claim native validation from cloud results.

## Automation acceptance

- Preserve idempotency/request keys, product revisions, durable steps, pause/cancel,
  explicit human approval, per-item error isolation and audit history.
- A request sent before interruption is uncertain; reconcile its external receipt
  before replay. Never automatically resend paid model calls or seller writes.
- Do not remove safety gates, skip failing tests, or replace assertions just to make CI green.
  If a behavior has intentionally changed, update the test with an explanation and
  meaningful checks of the new contract.
- Fake account/protocol tests prove software behavior only. `real_noon_verified`
  remains false until authorized account submission and offer readback are recorded.
- Do not equate content submission/QC with purchasability, `live_status`, orders or profit.
- Source facts, reference ownership and media rights must remain traceable.
- Never commit credentials, login sessions, .env files, real business databases,
  customer records, original media or local release bundles. The repository is public.

## Continuous improvement

Take one highest-priority unfinished item from `docs/AUTOMATION_ROADMAP.md` per run.
Reproduce, implement, run relevant regression tests and browser checks, then record
exact results in `docs/VERIFICATION.md`. Prefer a `codex/` branch and a reviewable PR.
Do not merge, spend, change seller accounts, buy inventory or publish listings without
specific authorization. Repository development and test automation are authorized.
Keep the roadmap broad; do not call an individual slice the complete platform.
Report new blockers accurately. Quiet follow-ups should notify only for a meaningful
change, a verified result, a failure or a required user action.
