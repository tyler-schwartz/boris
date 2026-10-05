---
name: boris
description: >-
  Implements tickets in a .scratch feature sequentially using the calling
  thread's agent and model. Use /boris with once for one ticket, afk for
  an unattended Docker Sandbox loop, or progress to inspect the queue.
disable-model-invocation: true
---

# Boris

Invocation: `/boris <feature> [once|afk|progress|status|stop] [iterations]`.
`once` is the default. `afk` defaults to a maximum of 20 tickets and stops
earlier when all tickets are complete. Implement exactly one ticket per agent
session; never start parallel workers. The executable selects the ticket,
launches the implementing agent, and checks the result. Do not implement tickets
in the calling chat.

## Launch

Infer the CLI provider and exact model from the **calling thread**. Codex threads
use `--agent codex`, Claude Code uses `--agent claude`, Cursor uses
`--agent cursor` even when its selected model is a Claude or OpenAI model.
Use explicit runtime/session metadata, or the thread's `modelSelection` when
available. In Cursor, prefer omitting `--model`: Boris reads the active
`CURSOR_CONVERSATION_ID` composer selection, including effort and fast settings.
Only pass a model explicitly when the thread exposes the actual CLI identifier
(or an unambiguous CLI display name). Human UI names are not CLI identifiers;
Boris resolves them using `agent models` before launching. Never invent a slug
or choose an effort level yourself. Do not infer the provider from the model's name. Do not substitute
another model. If the thread doesn't expose its model, omit `--model` and let
Boris attempt session discovery; report a discovery error rather than guessing.

Run from the target workspace, using the installed `boris` executable:

```bash
boris "<feature>" once --agent "<provider>" --model "<thread-model>"
boris "<feature>" afk --agent "<provider>" --model "<thread-model>"
```

For Cursor session discovery, use `boris "<feature>" afk --agent cursor`
(or `once`) without `--model`. If session discovery is unavailable, use the
thread's exact CLI identifier from `agent models`, including effort/fast suffixes.

Pass the iteration count after `afk` only if supplied by the user. Both modes
return after launching a detached worker. `once` runs locally with unattended
permissions; `afk` uses a named Docker Sandbox. Boris enforces a workspace lock,
so do not use `pgrep` or infer worker activity from Git changes. Do not relaunch
if startup fails or a worker is already running.

AFK requires a credential stored by `sbx`, separate from the host agent login.
If preflight reports missing authentication, relay its terminal setup command.
Do not initiate interactive OAuth or change credentials in this chat.

## Report progress in chat

After a successful launch, tell the user the feature, mode, resolved agent/model
and log location from Boris's output/status, not from your input arguments.
For ticket progress use the event's `ticket_position` and `total`, or its message
(e.g. “Ticket 1/4”). `iteration_limit` is only a stopping cap; never use it as
the feature's ticket count. Then monitor concise events, using tool timeouts longer than the wait:

```bash
boris "<feature>" follow --json --since 0 --wait 15
```

On subsequent calls replace `0` with the returned `sequence`. Track `run_id`;
if it changes, reset the sequence. `--since` avoids one thread consuming another
thread's progress. Each response contains `events`, `status`, and `exited`.

**Tool output is collapsed. Write meaningful updates as normal assistant
commentary.** Report the ticket starting, each ticket's completion count, sandbox
startup, and any error. Summarize the outcome and checks from
`event.summary` on completed tickets or `status.agent_summary` when present. Combine events from one poll into a short
update; do not copy JSON or raw tool transcripts into chat. If an event is only
a heartbeat, report elapsed time and last output age at most once a minute.
Empty polls need no commentary.

Continue until `exited` is true unless the user asks to leave it running. The
worker survives the calling chat ending. If leaving it running, give the status
and stop commands. On exit, report the terminal phase and actual outcome:
`complete` means every ticket is complete; `finished` means the one-ticket pass
finished; `blocked`, `failed`, `limit`, and `stopped` need their own explanation.
A successful launcher exit does not mean implementation succeeded.

For failure detail, inspect the log path in `status.log` and the adjacent
`worker.log`. `boris "<feature>" follow --verbose` reads new raw log output for
diagnostics; do not use it for routine chat progress. Do not restart a failed
run automatically.

## Inspect or stop

For `progress` or `status`, run `boris "<feature>" status --json`. Summarize
completed tickets, the running/next ticket, any blockers, and the completion
ratio. Ticket statuses and checked acceptance criteria are authoritative;
`progress.txt` supplies details of the work and checks. Existing progress files
can have different formats, so do not count their headings as completed tickets.
Inspection requires no agent/model and starts no implementing agent.

For `stop`, run `boris "<feature>" stop` and confirm the terminal status.
Stopping AFK stops its dedicated sandbox; interrupted work is retained for review.

## Queue contract

The feature needs `spec.md` and numbered `issues/<NN>-<slug>.md` files. Tickets
have `**Status:** ready-for-agent` and `**Blocked by:** None` or references such
as `01 First ticket, 02 Second ticket`. The lowest-numbered ready ticket whose
blockers are complete runs next. `done` and legacy `resolved` count as complete
only when no acceptance checkbox remains unchecked. No runnable ticket with
unfinished work is **blocked**, never complete. Boris stops if the agent exits
without finishing its selected ticket and updating `progress.txt`.

All tickets for a feature use ONE feature branch, created or reused before the
first ticket's implementation. Each completed ticket produces exactly ONE commit
after its checks pass, combining implementation and any tracked ticket/progress
bookkeeping. Later sessions and resumed `once`/`afk` runs reuse that branch;
never create per-ticket branches or separate bookkeeping commits. Record the
branch name and commit subject in `progress.txt` so fresh sessions retain this
context. Keep earlier tickets' commits intact.
