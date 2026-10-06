# Security

## Reporting a problem

Please report security problems privately, through GitHub's private vulnerability reporting: open
the repository's **Security and quality** tab and choose **Report a vulnerability** (or go straight to the
[report form](https://github.com/ishraq21/skyborne/security/advisories/new)). Don't open a public
issue for them. Private vulnerability reporting is turned on for this repository, so only the
maintainer can see what you send. You'll get an answer as soon as possible; fixes go into the latest
version.

## What Skyborne stores, and where

Skyborne keeps everything Claude Code's hooks send it, on your computer only:

- `~/.skyborne/skyborne.db` (SQLite): every hook event with its full JSON. That includes your
  prompts, Claude's answers, each tool call's input (commands, file paths, file contents being
  written) and each tool's output, cut to 20 KB per field. Also token counts per API message, read
  from Claude Code's transcripts, and each session's latest status line (cost, context, rate limits).
  If you run `skyborne import` (or say yes when Skyborne first starts), the same for your past
  sessions, read from the transcripts Claude Code already keeps in `~/.claude/projects`.
- The names you give districts in the city.
- How each permission request ended (the `decisions` table): the tool and its input (the command,
  file path or content Claude Code asked to use), the permission suggestions Claude Code offered,
  the answer, whether it was given in Skyborne or the terminal, and how long it waited.
- `~/.skyborne/backups/`: copies of your Claude Code `settings.json`, made before Skyborne changes
  its `statusLine` key. These can contain anything your settings hold, such as environment variables.
- `~/.skyborne/install.json`: what `skyborne install` changed, so `skyborne uninstall` can undo it.
- `~/.skyborne/state.json`: whether Skyborne already asked about importing past sessions.

Skyborne creates `~/.skyborne` readable only by your user account. Data older than 30 days is
deleted (`retention_days` in `~/.skyborne/config.json`). Deleting the folder deletes everything.

## How the server is exposed

- It listens on `127.0.0.1` only (IPv4 loopback), never on a network interface.
- Every request must name the server itself in its `Host` header (`127.0.0.1:<port>` or
  `localhost:<port>`), which blocks DNS-rebinding attacks from web pages.
- A browser request coming from any other page (an `Origin` header that isn't the server's own) is
  refused, and no CORS headers are sent, so websites you visit can't read or send events.
- Logs contain at most an event's or tool's name, the first 8 characters of a session id, and how a
  permission request was answered; never payloads.
- Skyborne makes no requests off your computer and has no telemetry. The city page, its 3D engine
  (three.js) and its fonts are served by Skyborne itself, and the page is sent with a
  Content-Security-Policy that lets the browser load nothing, and connect to nothing, outside it.
- Actions need the token created at each launch: the page gets it inside its own HTML, and again in
  `ready` each time it connects to `/events` (so an open page keeps working after Skyborne restarts),
  and sends it with every rename and every approve or deny (`X-Skyborne-Token`). Other websites can
  read neither the page nor the stream, so they can't get the token. An action without it, or from another `Origin`, is refused
  before anything is looked up, and changes nothing. A session's whole record (`/api/session` and
  `/api/step`: prompts, commands, tool input and output), which the page reads for a session's detail,
  needs the same token.
- Permission requests come in on `/permission`, from the plugin's `curl`. A browser can't post there:
  any request that carries an `Origin` header is refused.

Any program running on the same computer, under any user account, can connect to `127.0.0.1`:
it can read the live stream at `/events` (prompts, commands and answers included, and the token), post
events, and load the page (and with it the token, and so every session's whole record). **So it can also approve or deny the permission requests
that are waiting.** On a computer shared with people you don't trust, don't run Skyborne.

## Answering permission requests

- Skyborne never approves or denies anything on its own, and never on a timer. The only thing that
  answers a request is a click (or `A` / `D`) on its card in the city.
- The dialog in the terminal works alongside: whichever answer comes first wins. A "No" typed in the
  terminal makes Claude Code stop the waiting hook at once, so a later answer from the city can't
  reach it (docs/FINDINGS.md).
- A request nobody answers is held for `approval_timeout_seconds` (600 by default) less 5 seconds,
  then released with no answer: the dialog stays in the terminal.
- `claude -p` and Agent SDK runs are never held (on Windows without Git Bash, where the hook runs in
  PowerShell, this hasn't been tried and they may be held). `AskUserQuestion` and `ExitPlanMode` are never
  answered by Skyborne (their cards say to answer in the terminal). There's no "always allow".
- Every request that gets a card is logged in the `decisions` table when it ends. An answer from the
  page is shown as given only once the session's transcript records that Claude Code applied it; if
  the terminal had answered first, the card and the log say so.

## Recordings

`skyborne record` writes a file meant to be shared, so it scrubs it first: ids, keys and tokens,
home folders, your names, email addresses and the computer's name, and real times; with
`--stand-ins` also prompts, replies, file contents and tool output. It refuses to write the file if
a final check still finds anything. Scrubbing can't recognise a secret with no recognisable shape
or a private fact in plain words, so read the text it lists before sharing. Details:
[docs/RECORDING_FORMAT.md](docs/RECORDING_FORMAT.md).

## What Skyborne changes outside its folder

- `skyborne install` copies its plugin to `~/.claude/skills/skyborne` (or under
  `$CLAUDE_CONFIG_DIR`) and marks it as Skyborne's. Uninstall removes that folder only if the mark
  is there.
- Only if you agree, it sets the `statusLine` key of Claude Code's `settings.json`, after a backup.
  Uninstall restores the original file byte for byte when you haven't changed it since, and
  otherwise restores only that key.
