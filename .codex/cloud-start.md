# Noon cloud startup

During environment installation/startup, do not alter tracked application files,
tests or lockfiles. This does not restrict later user-authorized coding or PR work.
Read AGENTS.md, PRODUCT.md and docs/AUTOMATION_ROADMAP.md before coding.

The install script is `bash scripts/setup.sh`. It supports unprivileged Linux
when the image supplies fonts and Chromium system libraries. Do not use sudo,
disable TLS verification or copy macOS .venv/desktop bundles into Linux.

For each shell use `bash scripts/with-runtime.sh COMMAND ...`; installation shell
exports do not persist. Always use `.venv/bin/python` explicitly.
In account-prepared Cloud images, read `/workspace/.noonai-tools/start.md`
and `/workspace/.noonai-tools/install.sh` if present. They describe the current
published assets, including `/workspace/.noonai-assets/playwright`. The runtime
helper preserves an explicitly configured PLAYWRIGHT_BROWSERS_PATH and selects
prepared browser assets beside the checkout before its local cache fallback.
Verify browser launch after ordinary task creation; setup-chat verification alone
is insufficient. If the pinned browser is absent, use the official installer
`npx --prefix scripts/browser playwright install chromium` with the same exported
asset path. Do not invoke sudo/--with-deps in an unprivileged image or bypass TLS.
For sandbox-denied localhost test sockets, use the normal command approval flow;
report denial rather than changing security policy or claiming the smoke passed.

1. Run `bash scripts/with-runtime.sh .venv/bin/python scripts/doctor.py`.
2. Create `PREVIEW_DATA=$(mktemp -d)` in the startup shell. Never use workbench/data.
3. Start `bash scripts/with-runtime.sh .venv/bin/python workbench/server.py
   --data "$PREVIEW_DATA" --port 0 --ready-file "$PREVIEW_DATA/ready.json"`
   in the background, keeping its PID and logs under that temporary directory.
4. Wait for ready.json, read its actual URL, and check HTTP GET / returns 200.
   On startup failure stop and report the log; never kill unrelated processes.
5. Verify `bash scripts/with-runtime.sh .venv/bin/python scripts/smoke.py` and
   `bash scripts/with-runtime.sh node scripts/browser/smoke.cjs`.

Use `bash scripts/check.sh` for full regression, preserving all existing failures.
No actual seller or paid model calls during startup/tests; real_noon_verified stays
false. Linux checks do not validate Swift/macOS packaging.
