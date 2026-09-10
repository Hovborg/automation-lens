# Contributing to Automation Lens

Contributions are welcome when they keep the analyzer understandable, read-only toward Home Assistant, and honest about the limits of static analysis.

For suspected security vulnerabilities, follow the [security policy](SECURITY.md) and report privately instead of opening a public issue.

The command-line implementation lives in `automation_lens.py`; the installed command is `automation-lens`.

## Prepare a development environment

```powershell
git clone https://github.com/Hovborg/automation-lens.git
Set-Location .\automation-lens
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pytest -q
```

On Linux or macOS, use `python3` to create the environment and `.venv/bin/python` for the remaining commands.

## Keep test cases synthetic

Tests and issues must use fictional configuration and fake API responses. Never commit or attach:

- Home Assistant registry exports, real automations, scripts, traces, or JSON findings
- access tokens, cookies, Authorization headers, webhook IDs, or `.env` files
- private hostnames, IP addresses, coordinates, person names, device IDs, or household routines

Use names such as `light.demo_lamp`, URLs under `example.invalid`, and clearly fake values such as `test-token`. Reduce a bug to the smallest four-file fixture that still reproduces it. The included `examples/demo` folder shows the expected shapes.

## Preserve the safety boundary

- Home Assistant access must remain read-only: REST reads and WebSocket registry listing only.
- Do not add action calls, configuration writes, SSH fallbacks, telemetry, background services, or automatic token storage.
- Keep redirects rejected when authorization is present and keep TLS certificate validation enabled.
- Treat every finding as an investigation lead. A cycle, conflict, or missing reference must not be presented as proof of a live fault.
- A zero exit code means analysis completed; it must not imply that no findings exist.

## Validate a change

Run the synthetic suite:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Tests must not contact a real Home Assistant instance. For CLI changes, cover both readable output and `--json` where relevant. For parser or analyzer changes, include a minimal fictional input and assert the specific relationship or finding rather than copying a private export.

Run the bundled demo as a user-facing smoke test:

```powershell
.\.venv\Scripts\automation-lens.exe --data-dir examples\demo --findings
```

## Open a pull request

Describe the concrete behavior before and after the change, the command that verifies it, and any model limitation that remains. Keep unrelated refactors separate. Update the English and Danish guides together when commands, inputs, output, privacy behavior, or interpretation guidance changes.

The `main` branch is protected. Work on a separate branch and open a pull request, including for documentation changes.

- Keep the branch up to date with `main`. All four test jobs must pass: Linux and Windows, each with Python 3.10 and 3.14.
- CodeQL merge protection checks for new high or critical security alerts and code-quality errors in the pull request diff. GitHub currently excludes Dependabot pull requests analyzed by CodeQL default setup from this rule; the four test jobs and manual merge still apply. See [GitHub's documented limits](https://docs.github.com/en/code-security/concepts/code-scanning/merge-protection).
- Resolve outstanding review conversations before merging. No second reviewer is required, so the maintainer can still complete changes independently.
- Merge with **Squash and merge**. Automatic merging is disabled; merged branches are deleted automatically and can be restored on GitHub.

For fork pull-request workflows, the approval policy is set to require approval for all external contributors. Actions are limited to this owner's repositories and actions published by GitHub; introducing another action requires a separate review of the repository's Actions permissions.

By contributing, you agree that your contribution is licensed under the repository's MIT License.
