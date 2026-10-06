# Event format

How events reach Skyborne, how they're stored, and the live session document it builds from them.
The rules behind each field are in `skyborne/reducer.py`; the evidence for them is in
[FINDINGS.md](FINDINGS.md).

## In: what Claude Code sends

| Path | Sent by | Body |
| - | - | - |
| `POST /hook` | the plugin: `curl` run as a background (`async`) command hook for each event but `PermissionRequest` | the hook's JSON, exactly as Claude Code wrote it |
| `POST /permission` | the plugin: `curl` run as a waiting command hook for `PermissionRequest` (see "Permission requests" below) | the hook's JSON, exactly as Claude Code wrote it; headers `X-Skyborne-Wait` (seconds the server may hold it) and `X-Skyborne-Entrypoint` (Claude Code's `$CLAUDE_CODE_ENTRYPOINT`) |
| `POST /api/answer` | the city page, when you approve or deny a request | `{"id": "<request id>", "decision": "allow" \| "deny"}`. Needs the page's `X-Skyborne-Token` header and the server's own `Origin`, else `403`. `200 {"ok": true}` once the answer reached the hook (the card then shows "sent" until Claude Code confirms it, see `answer`); `409 {"ok": false, "reason": ...}` when it can't be taken: `terminal` (answered in the terminal first), `answered` (from a page already) or `closed` (the request is over) |
| `POST /statusline` | `skyborne statusline` (only if you installed the status line) | the status line's JSON |
| `POST /api/names` | the city page, when you rename a district | `{"id": "<session id>", "name": "..."}`; an empty name or `null` removes it. Needs the page's `X-Skyborne-Token` header and the server's own `Origin`, else `403` |

Events recorded: `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`,
`PostToolUseFailure`, `PermissionRequest`, `PermissionDenied`, `Notification`, `SubagentStart`,
`SubagentStop`, `Stop`, `StopFailure`, `PreCompact`, `PostCompact`, `SessionEnd`. `MessageDisplay`
is not used: Claude Code waits for that hook while text streams.

`/hook` and `/statusline` answer `200` with an empty body at once, whatever the body holds. Bodies
over 2 MB are dropped. `/permission` stores its event the same way, then holds the request until it's
answered (below). A request that carries an `Origin` header (any browser) is refused there.

### Permission requests

The `PermissionRequest` hook is the one hook that waits: `curl -sf -m N … /permission; exit 0`, run by a
shell, with hook `timeout` N (`approval_timeout_seconds` in `~/.skyborne/config.json`, default 600).
Claude Code opens its terminal dialog at the same time, and whichever answer comes first wins. The
server holds the request and answers it with one of:

- the page's decision, when you approve or deny it in the city:
  `{"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"allow"}}}`, or the
  same with `"behavior":"deny","message":"Denied from Skyborne"`;
- an empty body (no decision, so the dialog in the terminal decides) when the request is over without
  the page: the call ran or failed (a "Yes" typed in the terminal, seen when the tool finishes), its
  transcript shows it refused (a "No"), the session ended, the lead moved on (lead only) or the helper
  stopped (helpers only); when `X-Skyborne-Wait` seconds (N less 5) pass; or when the server stops.

Claude Code closing the connection (it stops the hook when you type "No") ends the request too.
Requests are never held, and get an empty body at once, from `claude -p` and the Agent SDK
(`X-Skyborne-Entrypoint` starting `sdk-`), and for `AskUserQuestion` and `ExitPlanMode`, which need
more than yes or no. When Skyborne isn't running, `curl` fails, `exit 0` ends the hook with no output,
and the dialog works as usual. The request is matched to its `PreToolUse` like the waiting state below.

Because each event is sent by its own background process, events a few milliseconds apart can
arrive out of order, and hook events carry no time of their own (Skyborne stamps the time they
arrive). Nothing in the document depends on arrival order: calls are matched by their ids, and the
tests shift every event by up to 30 ms and check the same state comes out.

`skyborne import` writes events too: the ones a past session's transcripts show, shaped like the
hooks' and marked `imported`, with the transcript lines' own times (see "Imported sessions" below).

## Stored: `~/.skyborne/skyborne.db` (SQLite)

| Table | One row per | Kept |
| - | - | - |
| `events` | hook event: `received_at` (ms), `session_id`, `event`, `agent_id`, `tool_use_id`, `payload` (the JSON; `tool_response` and `error` over 20 KB are cut to 20 KB with `{"skyborneTruncated": true, "bytes": n, "head": ...}`, keeping an Agent result's `agentId`, `resolvedModel`, `status` and `isAsync` beside it), `source` (`hook`; `imported` by `skyborne import`; or `transcript`: a `SessionEnd` with reason `continued` and `continued_in` (the other session's id), stored when a transcript's `continued-in` line says the conversation went on in another session, which no hook reports) | 30 days |
| `usage` | API message per session (`message.id`), from the transcripts: agent, model, the four token counts, whether it's the final line | 30 days |
| `results` | tool call whose transcript result was an error (`tool_use_id`, time): the only record of a "No" typed in the terminal | 30 days |
| `statusline` | session: its latest status line JSON | 30 days |
| `titles` | session: its names from the transcript, `custom` (set with `/rename` or `--name`) and `ai` (Claude Code's own title), and `at`, when one last changed | 30 days |
| `names` | session: the name you gave its district in the city | until you clear it |
| `imports` | session `skyborne import` brought in: when, from which transcript, how many events and messages | |
| `decisions` | permission request: `session_id`, `agent_id` (`main` for the lead), `tool_name`, `request` (`tool_input` and `permission_suggestions`), `asked_at`, `answered_at`, `decision` (`allow`, `deny`, `unknown` or `none`), `answered_in` (`skyborne`, `terminal` or `nowhere`), `how` (`page`, `ran`, `failed`, `refused`, `denied`, `hangup`, `prompt`, `idle`, `stopped`, `ended`, `server_stop`, or `error` if holding it failed), `timed_out`, `waited_ms`, and `confirmed`: for an answer from the page, `1` when the transcript showed Claude Code applied it, `0` when the call's result came without that (the terminal had answered first; `decision` and `answered_in` are then corrected), empty when it never said | 30 days |

The 30 days can be changed with `{"retention_days": N}` in `~/.skyborne/config.json`.
`SKYBORNE_HOME` moves the whole folder.

Besides the indexes by session and by time, `events` has one by session and `tool_use_id`, for
fetching one tool call (`GET /api/step`). For a "Yes" typed in the terminal, a decision's `answered_at`
is when the call's result was seen, so its `waited_ms` includes the tool's run time.

## Out: `GET /events`

[Server-Sent Events](https://html.spec.whatwg.org/multipage/server-sent-events.html). On connecting
you get, in this order:

1. `names`: every name you gave a district;
2. a `state` for each of the 60 most recent sessions, newest first: the live ones, and past ones
   rebuilt from the database (sessions recorded earlier, or imported);
3. `ready`: the backfill is complete. `token` is this launch's key, which the page sends with its
   actions; a page that reconnects after Skyborne restarts takes the new one, so it needs no reload;
4. `asks`: the permission requests waiting now.

Then a `state` each time a session changes (changes are gathered for at most 0.2 s), `names` when a
name changes, `gone` when a past session's history is deleted by the 30-day clean-up, `asks` whenever
the waiting requests change, and `answer` when it's known what came of an answer from the page. A comment
line (`: heartbeat`) every 15 seconds keeps the connection open. `GET /events?feed=N` sends only
each session's newest `N` feed items; the city page asks for 50. The stream holds the launch token (in
`ready`), so don't paste it raw into a bug report, though it works only on this computer and changes
at each restart.

```
event: names
data: {"names": {"<session id>": "Rate limits"}}

event: state
data: {"id": "<session id>", "doc": { ...the session document... }}

event: ready
data: {"token": "<this launch's token>"}

event: gone
data: {"id": "<session id>"}

event: asks
data: {"v": 7, "asks": [{"id": "<request id>", "session": "<session id>", "agent": "main", "tool": "Bash",
       "input": {"command": "npm test"}, "since": 1791060000000, "until": 1791060595000, "state": "open", "terminalOnly": false}]}

event: answer
data: {"id": "<request id>", "session": "<session id>", "agent": "main", "decision": "allow", "applied": true}
```

`asks` always carries the whole list; `v` grows with each list a server sends (a listener keeps the
newest, and starts over when it reconnects). `state` is `open` (the page can answer it), `sent` (the
page's answer reached the hook; waiting for Claude Code's word) or `terminal` (answer it in the
terminal: the server stopped waiting, or `terminalOnly`, a question or a plan). `since` is when the
request came in and `until` when the server stops holding it (`since` plus `X-Skyborne-Wait` seconds, or
595 s without that header), both ms; `until` is `null` for a request it never holds (`terminalOnly`).

After a page's answer is sent, the server reads the transcript Claude Code writes for that call (the
session's, or a helper's own `<session>/subagents/agent-<id>.jsonl`). A `hook_permission_decision` line
means Claude Code applied it: `answer` with `"applied": true`. The call's result with no such line
before it means the terminal had answered first (a "Yes" there doesn't stop the hook, so the page's
answer still reached it, too late): `"applied": false`. If the transcript says neither within 3 seconds
of the call ending, or 10 minutes after the answer, `"applied": null`. An `answer` comes before the
`asks` that drops the card.

A session quiet for 6 hours leaves the server's memory but stays a past session: its last document
keeps being sent to new listeners, and if it starts again (`claude --resume` keeps the session id)
its whole history is read back first.

## Out: a session's detail (`GET /api/session`, `GET /api/step`)

The city page's session detail reads a session's whole record on demand. Both need the page's
`X-Skyborne-Token` (the responses hold prompts, commands and tool output), a `Host` naming this server,
and no `Origin` but the server's own (a browser sends none on a same-origin `GET`); else `403`. An
unknown session or call gets `404`. Both are sent with `Cache-Control: no-store`.

`GET /api/session?id=<session id>`: built by `Session.detail()` in `skyborne/reducer.py` from the session
in memory, or rebuilt from the database for a past one (one rebuild at a time; the last 4 are kept).

| Field | Meaning |
| - | - |
| `id`, `updatedAt` | the session, and the newest thing heard about it (ms) |
| `agents[]` | `{id, name, role, type, parent, start, end}`: the lead (`id: "main"`) and every helper, with when it started and stopped (`end` is `null` while it runs) |
| `steps[]` | one per tool call, by start: `{id (tool_use_id), agent, tool, kind, text, start, end, ms, outcome, error}`. `kind` and `text` are as in the feed; `start` is the PreToolUse time, else when the call was first heard of; `end` the PostToolUse/PostToolUseFailure time, else the transcript's result, else the session's end if it ended, else `null` (running); `ms` the feed's `durationMs`; `outcome` `ok`, `failed`, `interrupted`, `denied` (auto mode), `refused` (a "No" in the terminal) or `null` |
| `conversation[]` | `{ts, who: "you" \| "claude", text}`: your prompts and the lead's final reply in each turn (the `Stop` hook's `last_assistant_message`); text written between tool calls isn't recorded |
| `files[]` | `{path, edits, failed}`: each file an `Edit`, `MultiEdit`, `Write` or `NotebookEdit` call named, how many of those calls ran and how many failed or were refused; edits made through `Bash` aren't seen |
| `approvals[]` | the session's `decisions` rows: `{tool, agent, text, askedAt, answeredAt, decision, answeredIn, how, waitedMs, timedOut, confirmed}` (`text` is the request in one line, as in the feed) |

`GET /api/step?session=<session id>&id=<tool_use_id>`: one call as stored, `{tool, input, output,
error}`, each only when an event had it: `input` is `tool_input`, `output` the PostToolUse's
`tool_response`, `error` the PostToolUseFailure's `error` or the PermissionDenied's `reason`
(either with the `skyborneTruncated` marker when it was over 20 KB).

## The session document (`v: 2`)

The document keeps full text everywhere, every feed item and every helper (finished ones stay, as
`done`).

| Field | Meaning |
| - | - |
| `v` | `2` |
| `title` | the session's folder name |
| `sessionName` | the name you gave the session (the transcript's newest `/rename`), else the newest name a hook (`claude -n`) or the status line (`session_name`) sent, else Claude Code's own title for it (the transcript's `ai-title`); missing when there's none |
| `headline` | the latest of: your last prompt, the lead's last answer, "Hit a snag" after an API error; else "Continued from another session" for a fork, else "No prompt yet" |
| `startedAt`, `updatedAt` | ms timestamps: the first and the latest thing Skyborne heard about this session; a fork starts at its `SessionStart` (its transcript opens with a copy of the conversation it came from, old times and all). A title never moves `updatedAt`: its lines carry no time |
| `turns` | prompts you sent (a helper's report, `<task-notification>`, is not a turn) |
| `tokens` | `total`, `in`, `out`, `cw` (cache written), `cr` (cache re-read), summed over the lead's and every helper's transcript, each API message counted once; `helpers[]`: `{id, name, role, usage, model}` per helper. A fork counts only messages after its `SessionStart`: the copied ones are counted in the session they came from |
| `context` | `{tokens, window, percent}` from the status line, when installed |
| `cost` | `{usd}` from the status line: Claude Code's own figure, which is larger than the transcript token sum |
| `rateLimits` | the status line's `rate_limits` (`five_hour`, `seven_day`), when Claude Code sends them |
| `waiting` | `{agent, tool, since}` while an approval dialog is open, else `null` |
| `agents[]` | the lead (`id: "main"`) and every helper, see below |
| `feed[]` | everything that happened, newest first, see below |
| `ended` | `{at, reason}` once the session has ended; gone again if it's resumed. `reason` is the `SessionEnd`'s, or `continued` with `continuedIn` (the other session's id) when the conversation was moved to the background; that wins over the `SessionEnd` that `/bg` sends right after it |
| `imported` | `true` when the session was rebuilt by `skyborne import` |

### `agents[]`

| Field | Meaning |
| - | - |
| `id` | `main` for the lead, else the helper's agent id |
| `name` | "Skybot" for the lead ("Claude Bot" in older recordings); helpers are named by role and number: "Scout 1", "Builder 2"… |
| `role`, `type` | "Lead Agent"/`lead`, or `<agent type> Agent` and the agent type (`Explore`, …) |
| `status` | `working`, `idle`, `done` or `error` |
| `kind` | what it's doing, from the tool's name only: `think`, `bash`, `read`, `edit`, `write`, `search`, `web`, `spawn`, `task`, `mcp`, `tool`, `wait`, `idle`, `done`, `error`, `leave` |
| `tool`, `activity`, `activitySince` | the tool in use, one line about it, and since when |
| `parent` | for a helper: the agent whose Agent call launched it |
| `description` | for a helper: the description its Agent call gave |
| `waiting` | true while this agent waits for your approval |
| `tools` | tool calls made |
| `usage` | `{in, out, cw, cr}` from its own transcript |
| `model`, `models` | the latest model, and the history (repeats and `<synthetic>` left out) |

### `feed[]`

`{ts, agent, agentId, kind, text}`, plus `tool`, and `toolUseId` and `durationMs` for tool
calls. `durationMs` is Claude Code's `duration_ms` (run time, without approval waits), or PostToolUse
time minus PreToolUse time when that's missing. A failed call adds an `error` item with Claude Code's
error text; a call refused in the terminal adds "<tool> didn't run".

## How the main states are decided

- **A helper** appears at `SubagentStart` (or from its `.meta.json` sidecar when rebuilt from
  transcripts), is linked to the Agent call that launched it (the call's `tool_response.agentId`,
  the sidecar's `toolUseId`, or its task notification), and is `done` at `SubagentStop`, not when
  the Agent call returns (helpers run in the background). A `SubagentStop` for an agent that never
  started is one of Claude Code's own agents and is ignored.
- **The lead** is `working` after a prompt or tool call; still `working` ("Waiting for 2 helpers")
  after a `Stop` while helpers it launched still run; `done` after its last `Stop`; `error` after
  `StopFailure`; `idle` after `Notification` `idle_prompt`, a manual `/compact`, a call refused in
  the terminal, or an Esc ("Interrupted": no hook fires, the transcript's "[Request interrupted by user…]"
  line says so; it isn't stored, so a session rebuilt from the database, as in a recording or a past
  session after Skyborne restarts, doesn't show it).
- **Waiting**: a `PermissionRequest` is matched to the `PreToolUse` with the same session, agent,
  tool and input, nearest in time (Claude Code 2.1.289 sends it without the `tool_use_id` the docs
  list; when one is there, it's used). It ends when that call settles (`PostToolUse`,
  `PostToolUseFailure`, `PermissionDenied`, or the transcript's `tool_result`, which is the only
  sign of a "No" typed in the terminal), at your next prompt, when the agent stops (an Esc stops the
  lead), when Claude Code reports the session idle, or when the session ends.
- **Endings are final**: once a helper stops, or the session ends, nothing stamped after it (a hook
  a few milliseconds late) changes that agent, unless the helper starts again or the session is
  resumed (a `SessionStart` after the `SessionEnd`). A resumed session can wait for you again.

## Imported sessions

`skyborne import --days N` reads each session transcript changed in the last `N` days (and its
helpers' transcripts) and stores what a live recording would have held, with `source = imported`:
`SessionStart` at the first line that has a time, `UserPromptSubmit` for each prompt you typed (and
each helper report), `PreToolUse` for each tool call, `PostToolUse`/`PostToolUseFailure` for each
result (a call refused in the terminal gets no event, as live, only a `results` row), `SubagentStart`
and `SubagentStop` for each helper, `Stop` at each message that ended a turn, and `SessionEnd` with
reason `imported` at the last line that has a time. That end only counts while nothing follows it: a
session still open when it was imported goes on live with its next hook. Plus the usage of every API
message, counted once. Lines without a time of their own (Claude Code writes several) are never given "now".
Transcripts hold no approval requests, tool run times or status lines, so imported sessions don't
have them; slash commands and `!` shell lines are not turns. Sessions already recorded live, or
imported before, are skipped.
