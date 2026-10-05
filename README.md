# Boris

A sequential implementer for `.scratch/<feature>/spec.md` and numbered
`issues/*.md` tickets. Requires Python 3.9+ and the selected agent CLI for local
runs, or Docker Sandboxes (`sbx`) for AFK runs. The `boris` Bash entrypoint locates
its Python runtime through symlinks; keep `lib/` beside the script when installing.

## Install

On macOS or Linux, with Bash, Python 3.9+, `curl`, and `tar` installed:

```sh
curl -fsSL https://raw.githubusercontent.com/tyler-schwartz/boris/main/install.sh | sh
```

The installer runs as your user, without `sudo`. It installs the root `boris`
script and its `lib/` runtime into `~/.boris`, then links
`~/.local/bin/boris` to the script. If `~/.local/bin` is missing from `PATH`, it
adds it to your shell's startup files (Zsh, Bash, Fish, or `~/.profile` for other
shells). Open a new terminal afterward, or for Bash/Zsh/POSIX shells run:

```sh
export PATH="$HOME/.local/bin:$PATH"
boris --help
```

Every skill directory in the repository's `skills/` directory is copied to
`~/.agents/skills/<name>`, including its supporting files. Rerun the same command
to update Boris and the bundled skill files. Existing files with the same names
are overwritten; unrelated skills and extra files are preserved. Skills added
to the repository later will automatically be included in future installs.

The installer downloads a single archive of `main`. To install a particular
tag or commit, fetch the installer from that revision and pass the same ref:

```sh
curl -fsSL https://raw.githubusercontent.com/tyler-schwartz/boris/<ref>/install.sh | BORIS_REF=<ref> sh
```

The selected agent CLI and Docker Sandboxes are installed separately.

## Usage

```sh
boris checkout-taxes once --agent codex --model <calling-thread-model>
boris checkout-taxes afk 20 --agent cursor --model <calling-thread-model>
boris checkout-taxes status
boris checkout-taxes follow --wait 15
boris checkout-taxes stop
```

The skill infers and passes the provider and exact model from its calling thread.
The CLI also accepts `BORIS_AGENT` / `BORIS_MODEL`, or attempts existing T3,
Claude, and Cursor session discovery when arguments are missing. Discovery is
bounded and never silently chooses a different provider. Cursor model arguments
are validated against `agent models`; unambiguous display names are mapped to
the CLI identifier. Cursor session discovery also preserves effort/fast settings
and fails rather than silently dropping an unavailable setting. Prefer omitting
`--model` in a Cursor thread so the active composer supplies its full selection. Cursor's provider is
`cursor` regardless of which vendor supplies its selected model.

Both modes launch a detached worker and return immediately. `once` implements
one ticket locally; `afk` starts/reuses a dedicated sandbox for that workspace
and provider, then invokes one fresh agent session per ticket, in sequence.
A filesystem lock prevents concurrent Boris workers across features in the same
workspace. Locks are released automatically when the worker exits.

Use `--foreground` to stay attached to milestones in a terminal, and add
`--verbose` to see the agent's full output. Startup defaults to a 180-second
limit, each ticket to 3600 seconds; adjust with `--startup-timeout` and
`--ticket-timeout`. Ticket progress uses the feature count (for example `Ticket 1/4`), with the
iteration cap stored separately. Thirty-second heartbeats include elapsed time and last
output age. Errors and timeouts stop the loop; incomplete work remains for
review. A cap defaults to 20 tickets, matching the skill. Launching successfully
is distinct from the worker finishing successfully.

## Sandbox setup

Host CLI authentication is separate from sandbox credentials. Run the command
for your provider in a terminal before using AFK:

```sh
sbx secret set openai --oauth
sbx secret set anthropic
sbx secret set cursor
```

Boris checks the stored credential and daemon before launching; it never waits
for an invisible login prompt. Credentials remain managed by `sbx`.

AFK uses `sbx run --detached --name ...` for startup and `sbx exec --workdir ...`
for the explicit agent command, without a pseudo-terminal. This separates VM
startup from execution and preserves stdout, stderr, and exit codes. The old
`sbx run cursor ... -- -p ...` launch reproduced an immediate agent exit with
no useful output in a non-interactive smoke test on sbx 0.46.0. Explicit exec
then exposed the underlying error: the stored Cursor API key was invalid.
Credential presence does not prove validity; runtime errors are retained in
the log and terminal status.
Docker documents both [non-interactive exec](https://docs.docker.com/reference/cli/sbx/exec/)
and [sandbox authentication](https://docs.docker.com/ai/sandboxes/agents/codex/).

Sandboxes stay available for reuse and inspection after ordinary completion.
`stop` and timeouts stop the dedicated sandbox to terminate remote execution.
Project code and `.scratch` are shared with the host. Dependencies and agent
configuration inside the sandbox can differ from the host, so checks must run
there. Local `once` uses unattended agent permissions as the existing launcher
did; run it in a workspace you trust.

## Queue and completion

Tickets use the `/to-tickets` format:

```md
# 01: First slice
**Blocked by:** None (can start immediately)
**Status:** ready-for-agent
- [ ] Acceptance criterion
```

Blockers can list ticket numbers and titles separated by commas or newlines.
The runner chooses the lowest-numbered ready ticket with completed blockers;
it does not ask the model to choose the next task. Completion requires `done`
(or legacy `resolved`) with every acceptance checkbox checked. Missing or
unknown blockers and malformed tickets fail explicitly. An unfinished queue
without a runnable ticket is blocked. Model completion promises are ignored.

After every agent exit Boris checks that exactly its chosen ticket transitioned
to complete and `progress.txt` changed. It stops on missing bookkeeping or
unfinished work instead of repeatedly spending iterations on the same ticket.
All tickets use **one feature branch**, created or reused before implementing
the first ticket. Each completed ticket produces **one commit**, made after its
checks and bookkeeping are complete. That commit includes the implementation
and any tracked ticket/progress updates; ignored `.scratch` files stay ignored.
Later tickets and resumed `once`/`afk` runs reuse the same branch and preserve
earlier ticket commits. Four completed tickets mean four commits on one branch.

The agent records the branch name, commit subject, and verified checks in the
ticket/progress bookkeeping, then reports the commit hash in its final summary.
Using the subject avoids a second commit just to record the commit's own hash.
Boris validates queue bookkeeping; it does not independently audit the
implementation, tests, or Git history.

## Visibility

Each run keeps `agent.log`, `worker.log`, and `events.jsonl` under
`.scratch/<feature>/.boris/<run-id>/`. `boris.log` points at the current raw log;
previous runs remain available. `boris.status.json` records PID, provider/model,
phase, current ticket, last output, final agent summary, and exit code. Keep
runtime files out of version control, especially logs with project context.

`status --json` returns the current worker state and queue without requiring
agent/session discovery. `follow` returns concise events and an exit marker.
For independent consumers, use `follow --json --since <sequence> --wait 15`;
track the returned sequence and reset it for a new `run_id`. Without `--since`,
Boris remembers a per-feature event cursor. `follow --verbose` has a separate
byte cursor for diagnostic raw output.

The skill writes milestone summaries in normal assistant commentary, where
users can see them without expanding tool calls. A CLI cannot inject unsolicited
messages into a chat after that chat stops monitoring; the worker keeps running
and `status`/`progress` can be queried later.

Run the isolated lifecycle tests with `python3 -m unittest discover -s tests -v`.
