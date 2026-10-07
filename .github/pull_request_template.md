## Why

<!-- What problem does this solve? Link the issue it comes from. New features need one (see CONTRIBUTING.md). -->

## What changed

<!-- Bullet points of what was done. -->
-

## Tested

<!-- How did you check it works? -->

## Checklist

- [ ] It keeps the three promises: Claude Code only, local only, no telemetry or cloud.
- [ ] I opened an issue first if this is a new feature.
- [ ] I added or updated tests, and `python -m pytest` passes.
- [ ] If I changed `page/`, I ran `node build.js`, committed `skyborne/web/` and `page/dist/preview.html`, and `node tests/smoke.js && node tests/live-smoke.js` pass.
- [ ] I checked the city, the console, reel mode, Safe to film, sample districts and renaming still work.
- [ ] I updated the README and docs where behaviour changed.
- [ ] I added a line under `[Unreleased]` in CHANGELOG.md if users would notice the change.
- [ ] I checked any Claude Code field or event I used against the docs or a real payload.
- [ ] No real session data, secrets or personal paths are in the diff.

<!-- Made with Claude Code? Say so here. You still own every line. -->
