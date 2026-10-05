#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON="${NOON_PYTHON:-.venv/bin/python}"
"$PYTHON" scripts/doctor.py
"$PYTHON" -m unittest discover -s workbench/tests -v
"$PYTHON" -m unittest discover -s desktop/tests -v
"$PYTHON" scripts/smoke.py
for script in workbench/static/*.js browser-extension/domestic-capture/*.js; do node --check "$script"; done
node scripts/browser/smoke.cjs
node scripts/browser/navigation.cjs
node scripts/browser/minimax_subscription.cjs
node scripts/browser/scheduler.cjs
node scripts/browser/collection.cjs
node scripts/browser/catalog_campaign.cjs
node scripts/browser/erp.cjs
node scripts/browser/bank.cjs
node scripts/browser/fulfillment.cjs
node scripts/browser/after_sales.cjs
node scripts/browser/procurement.cjs
node scripts/browser/batch_edit.cjs
node scripts/browser/alerts.cjs
node scripts/browser/inventory_counts.cjs
node scripts/browser/shipping_manifests.cjs
node scripts/browser/pricing_plans.cjs
node scripts/browser/ad_analytics.cjs
node scripts/browser/import_profiles.cjs
node scripts/browser/domestic_capture.cjs
node scripts/browser/domestic_extension.cjs
node scripts/browser/domestic_sources.cjs
node scripts/browser/source_inbox_errors.cjs
node scripts/browser/submit_reconciliation.cjs
node scripts/browser/uncertain_model_retry.cjs
node scripts/browser/supplier_quotes.cjs
node scripts/browser/fx_registry.cjs
node scripts/browser/replenishment.cjs

# Integrated deepening workflows use synthetic temporary workspaces.
for workflow in domestic_capture catalog_groups supplier_quotes replenishment analytics alerts; do
  node "scripts/browser/${workflow}_deepening.cjs"
done
