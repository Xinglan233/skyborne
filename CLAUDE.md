# Skyborne — ground rules

Skyborne is an open-source (Apache 2.0), local-only monitor and tracer for Claude Code. A 3D sky city shows every session and agent live, keeps a full history, and lets the user approve or deny permission prompts.

## Scope
- Skyborne monitors Claude Code only; no support for Codex or other runtimes.
- Open an issue before starting a new feature (unless the maintainer asked for it).

## Existing code — build on it, don't rebuild
- The page (3D city, console, reel mode, safe-to-film, sample districts, token breakdown, renaming) already exists in page/src and is built by page/build.js. Keep that build. Keep every feature working.
- The collector is plugin/ + the local server, never a mod (an earlier mod-based system is retired).
- Change only what the task needs; list any change to existing behaviour and why before editing.
- Before finishing any change: check city, console tabs, reel mode, safe-to-film, samples and renaming still work, and list anything that broke.

## Verify before building
- Check current Claude Code docs (code.claude.com/docs) or real recorded payloads before using any field, event or flag. Never guess names. Note the source in the commit message.
- If docs and observed behaviour disagree, trust observed behaviour and record it in docs/FINDINGS.md.

## Local only
- At runtime Skyborne makes zero network requests off the machine. No telemetry. All assets (three.js, fonts) are bundled.
- The demo site is a separate static build that plays a scrubbed recording.

## Never get in Claude Code's way
- Hook endpoints answer within 50 ms with 200 and an empty body (except held approvals). Errors are swallowed and logged.
- Hooks use short timeouts. If Skyborne is down, Claude Code behaves exactly as if it was never installed.
- No MessageDisplay hook (a slow answer freezes the user's terminal).

## Security
- Listen on 127.0.0.1 only (IPv4). Check Host and Origin on every request. Actions (approve, deny, rename) need the per-launch token.
- Never auto-approve anything. Log every decision.

## User's settings
- Hooks ship inside plugin/, not in ~/.claude/settings.json. If settings.json must change (e.g. the status line), ask first, back it up, touch only our key, and restore it on uninstall.

## Code
- Server: Python 3.11+ standard library only. Tests: pytest. Ask before adding any dependency.
- Page: existing page/src + build.js. Its smoke test must keep passing.

## Every change
- Add or update tests; `pytest` and the page smoke test pass; CI green.
- Before each commit: one self-review of the diff in a fresh agent session or a review subagent; fix what it finds, then move on.
- Evals: scrubbed recorded sessions in tests/fixtures replay through the reducer and match saved expected results.
- Fact-check: every claim in README/docs matches actual behaviour; update them in the same change.
- Real session data never enters git unscrubbed. SPIKE_REPORT.md stays out of git; its scrubbed findings live in docs/FINDINGS.md.

## Borrowing
- Agent Flow (Apache 2.0) may be borrowed from: keep its license notice, list it in NOTICE, mark changes. Never use its name or logo.

## Ask first
- Deleting files, touching global settings, adding dependencies, changing the event format, any git push.
