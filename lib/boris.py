"""Sequential ticket runner. Standard library only; macOS and Linux."""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import time
import uuid

from cursor_models import resolve_model

TERMINAL = {'complete', 'finished', 'failed', 'blocked', 'limit', 'stopped', 'interrupted'}


class BorisError(Exception):
    pass


def read_json(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def atomic_json(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2) + '\n')
    tmp.replace(path)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def field(text, label):
    match = re.search(r'^\s*(?:\*\*)?' + label + r':(?:\*\*)?\s*(.*)$', text, re.M | re.I)
    if not match:
        return None
    value = match.group(1).strip()
    # Blockers may also be a short Markdown list below the label.
    if label == 'Blocked by':
        rest = text[match.end():].split('\n\n', 1)[0]
        if rest.strip() and not re.search(r'Status:|\[ \]', rest):
            value += '\n' + rest.strip()
    return value.strip()


def tickets(directory):
    result = []
    for path in directory.joinpath('issues').glob('*.md'):
        match = re.match(r'^(\d+)[-_.]', path.name)
        if not match:
            raise BorisError(f'ticket filename must start with a number: {path.name}')
        text = path.read_text()
        status = field(text, 'Status')
        if not status:
            raise BorisError(f'{path.name}: missing Status')
        status = status.lower().strip('` ')
        blockers = field(text, 'Blocked by')
        if blockers is None:
            raise BorisError(f'{path.name}: missing Blocked by')
        ids = []
        if blockers and not re.match(r'^none\b', blockers, re.I):
            # Numbers at the beginning of references, not incidental numbers in titles.
            ids = [int(n) for n in re.findall(r'(?:^|[,;\n]|\band\b)\s*(?:[-*]\s*)?(?:ticket\s*)?#?(\d+)\b', blockers, re.I)]
            if not ids:
                raise BorisError(f'{path.name}: cannot parse blockers: {blockers}')
        criteria = re.split(r'^## (?:Comments|Answer)\b', text, flags=re.M)[0]
        unchecked = bool(re.search(r'^\s*[-*] \[ \]', criteria, re.M))
        title = re.search(r'^#\s+(.+)', text, re.M)
        result.append(dict(id=int(match[1]), path=path, title=title[1] if title else path.stem,
                           status=status, blockers=ids, done=status in {'done', 'resolved'} and not unchecked))
    result.sort(key=lambda t: t['id'])
    if not result:
        raise BorisError(f'no tickets in {directory}/issues')
    by_id = {t['id']: t for t in result}
    if len(by_id) != len(result):
        raise BorisError('duplicate ticket numbers')
    for ticket in result:
        for blocker in ticket['blockers']:
            if blocker not in by_id:
                raise BorisError(f"{ticket['path'].name}: unknown blocker {blocker}")
    return result


def frontier(queue):
    done = {t['id'] for t in queue if t['done']}
    return next((t for t in queue if t['status'] == 'ready-for-agent' and set(t['blockers']) <= done), None)


def locate(feature):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', feature):
        raise BorisError('feature must be a single .scratch directory name')
    for root in [Path.cwd(), *Path.cwd().parents]:
        directory = root / '.scratch' / feature
        if directory.is_dir():
            if not directory.joinpath('spec.md').is_file():
                raise BorisError(f'missing {directory}/spec.md')
            if directory.resolve().parent != (root / '.scratch').resolve():
                raise BorisError('feature directory must stay inside .scratch')
            return root.resolve(), directory.resolve()
    raise BorisError(f'missing .scratch/{feature}')


def acquire(root):
    handle = open(root / '.scratch' / '.boris.lock', 'a+')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.seek(0)
        owner = handle.read().strip()
        handle.close()
        raise BorisError(f'a Boris worker already owns this workspace: {owner or "starting"}')
    return handle


def lock_busy(root):
    try:
        handle = acquire(root)
    except BorisError:
        return True
    handle.close()
    return False


def resolve(args):
    agent = args.agent or os.environ.get('BORIS_AGENT')
    model = args.model or os.environ.get('BORIS_MODEL')
    if agent and model:
        return agent, resolve_model(model) if agent == 'cursor' else model
    # Session discovery is a fallback for direct CLI use; the skill passes context explicitly.
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('session.py'))],
                                capture_output=True, text=True, timeout=25)
    except subprocess.TimeoutExpired:
        raise BorisError('session discovery timed out; pass --agent and --model from the calling thread')
    if result.returncode:
        raise BorisError((result.stderr.strip() or 'cannot infer this session') +
                         '; pass --agent and --model from the calling thread')
    inferred_agent, inferred_model = result.stdout.strip().splitlines()[:2]
    if agent and agent != inferred_agent and not model:
        raise BorisError('explicit agent differs from the session; pass its --model too')
    agent, model = agent or inferred_agent, model or inferred_model
    return agent, resolve_model(model) if agent == 'cursor' else model


def sandbox_name(root, agent):
    return 'boris-' + agent + '-' + hashlib.sha256(str(root).encode()).hexdigest()[:12]


def capture(command, timeout=20):
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise BorisError(f'{command[0]} is not installed')
    except subprocess.TimeoutExpired:
        raise BorisError(f'{" ".join(command[:3])} timed out; run sbx diagnose')
    if result.returncode:
        raise BorisError(result.stderr.strip() or result.stdout.strip() or f'{command[0]} exited {result.returncode}')
    return result.stdout


def preflight(root, args, agent):
    if agent not in {'codex', 'claude', 'cursor'}:
        raise BorisError(f'unsupported agent: {agent}')
    if args.mode != 'afk':
        from shutil import which
        binary = 'agent' if agent == 'cursor' else agent
        if not which(binary):
            raise BorisError(f'{binary} is not installed')
        return
    name = sandbox_name(root, agent)
    service = {'codex': 'openai', 'claude': 'anthropic', 'cursor': 'cursor'}[agent]
    data = json.loads(capture(['sbx', 'secret', 'ls', '--json']))
    available = any(s.get('name') == service and s.get('scope') in {'global', name}
                    for s in data.get('secrets', []))
    if not available:
        setup = f'sbx secret set {service}' + (' --oauth' if agent == 'codex' else '')
        raise BorisError(f'AFK needs a stored {service} sandbox credential. Run `{setup}` in a terminal first. '
                         'Host agent login is separate from sandbox authentication.')
    # Also verifies that sandboxd responds before detaching.
    capture(['sbx', 'ls', '--json'])


def agent_command(agent, model, prompt, sandbox=False):
    if agent == 'codex':
        return ['codex', 'exec', '--dangerously-bypass-approvals-and-sandbox', '--json', '--model', model, prompt]
    if agent == 'claude':
        return ['claude', '--dangerously-skip-permissions', '--print', '--verbose',
                '--output-format', 'stream-json', '--model', model, prompt]
    return [('cursor-agent' if sandbox else 'agent'), '--print', '--force', '--trust', '--sandbox', 'disabled',
            '--output-format', 'stream-json', '--model', model, prompt]


def prompt_for(directory, ticket, completed):
    if completed:
        branch_instruction = f'''{completed} feature ticket(s) are already complete. Continue on the SAME feature branch used for those tickets.
Read its name from progress.txt and confirm the current branch. Do not create a ticket branch or a new feature branch.'''
    else:
        branch_instruction = '''No feature tickets are complete yet. Before implementation, reuse the feature branch already created for this feature.
If it has not been created, create ONE appropriate feature branch unless project/user instructions prohibit it.
This branch is shared by ALL tickets in this feature; do not create a branch per ticket.'''
    return f'''Implement exactly ONE ticket: {ticket['path']} ({ticket['title']}).
Read {directory}/spec.md, that ticket, {directory}/progress.txt if present, and relevant project instructions.
Boris selected this ticket because its blockers are complete. Do not select or implement another ticket.
Work sequentially; do not delegate implementation to other agents.
If the behavior already exists, verify it rather than reimplementing it.
{branch_instruction}
Keep this branch checked out when you finish. These feature-wide branch and commit instructions apply to both once and afk, including resumed runs.
Implement the ticket and run the checks required by its acceptance criteria, including relevant tests and type checks.
In that ticket, check only criteria you verified. Set Status to done only if ALL criteria pass.
Append a ## Comments entry with the planned commit subject and verified checks. If blocked, leave Status ready-for-agent and explain why.
Append a dated entry for this ticket to {directory}/progress.txt with the outcome, feature branch name, commit subject, and checks; create it if missing.
Only after implementation, checks, and bookkeeping are complete, create exactly ONE commit for this completed ticket on the shared feature branch.
Include this ticket's code and tracked ticket/progress bookkeeping together in that commit. Do not make separate implementation, fixup, or bookkeeping commits.
Do not include unrelated user changes or Boris runtime files. Do not force-add ignored .scratch files.
If verification shows the behavior already exists and there are no tracked changes for this ticket, use an empty commit to record its completion.
Do not amend or squash earlier tickets' commits: N completed tickets must produce N commits on ONE feature branch.
Do not modify other ticket statuses. Stop after the single commit and give a short outcome summary with the branch and commit hash.
Use the commit subject in tracked bookkeeping so recording this commit's own hash does not require another commit.
Never claim feature completion: Boris checks the queue after you exit.
'''


class Run:
    def __init__(self, root, directory, args, state):
        self.root, self.directory, self.args, self.state = root, directory, args, state
        self.log = open(state['log'], 'a', buffering=1)
        self.events = open(state['events'], 'a', buffering=1)
        self.child = None
        self.seq = 0
        self.last_activity = time.monotonic()

    def event(self, kind, message, **updates):
        self.seq += 1
        self.state.update(updates, phase=kind, message=message, updated_at=now(), sequence=self.seq)
        atomic_json(self.directory / 'boris.status.json', self.state)
        event = dict(sequence=self.seq, time=now(), type=kind, message=message)
        if kind == 'ticket':
            event.update(ticket_number=updates['ticket_number'], ticket_position=updates['ticket_position'],
                         total=updates['total'], iteration=updates['iteration'], iteration_limit=updates['iteration_limit'])
        if kind == 'ticket_done':
            event['summary'] = self.state.get('agent_summary', '')
        self.events.write(json.dumps(event) + '\n')
        if self.args.foreground:
            print(message, flush=True)

    def output(self, line):
        self.log.write(line)
        self.state['last_output_at'] = now()
        self.output_tail = (getattr(self, 'output_tail', '') + line)[-2000:]
        if self.args.foreground and self.args.verbose:
            print(line, end='', flush=True)
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            return False
        if not isinstance(event, dict):
            return False
        kind = event.get('type')
        failed = kind in {'error', 'turn.failed'} or (kind == 'result' and event.get('is_error'))
        if failed:
            self.state['agent_error'] = event.get('error') or event.get('result') or event
        # Keep the final human summary out of the raw tool stream.
        item = event.get('item') or {}
        summary = ''
        if kind == 'item.completed' and item.get('type') == 'agent_message':
            summary = item.get('text', '')
        if kind == 'result' and not failed:
            summary = event.get('result', '')
        if kind == 'assistant':
            content = (event.get('message') or {}).get('content') or []
            summary = '\n'.join(part.get('text', '') for part in content
                                if isinstance(part, dict) and part.get('type') == 'text')
        if isinstance(summary, str) and summary:
            self.state['agent_summary'] = summary[-4000:]
        return failed

    def terminate_child(self):
        if not self.child or self.child.poll() is not None:
            return
        # sbx exec does not reliably stop remote children when its host CLI dies.
        if self.args.mode == 'afk':
            try:
                subprocess.run(['sbx', 'stop', self.state['sandbox']], stdin=subprocess.DEVNULL,
                               stdout=self.log, stderr=self.log, timeout=15)
            except (subprocess.TimeoutExpired, OSError):
                self.log.write('boris: could not confirm sandbox stop; inspect sbx ls\n')
        try:
            os.killpg(self.child.pid, signal.SIGTERM)
            self.child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(self.child.pid, signal.SIGKILL)
            self.child.wait()
        except ProcessLookupError:
            pass

    def command(self, command, timeout, stage):
        self.log.write(f'boris: {stage}\n')
        self.output_tail = ''
        env = dict(os.environ)
        env.pop('CLAUDECODE', None)
        env.pop('CLAUDE_CODE_CHILD_SESSION', None)
        self.child = subprocess.Popen(command, cwd=self.root, env=env, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        selector = selectors.DefaultSelector()
        selector.register(self.child.stdout, selectors.EVENT_READ)
        started = heartbeat = time.monotonic()
        self.last_activity = started
        buffer = b''
        failed = False
        try:
            while selector.get_map():
                for key, _ in selector.select(timeout=0.5):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        break
                    self.last_activity = time.monotonic()
                    buffer += chunk
                    while b'\n' in buffer:
                        line, buffer = buffer.split(b'\n', 1)
                        failed |= self.output(line.decode(errors='replace') + '\n')
                elapsed = time.monotonic() - started
                if elapsed > timeout:
                    raise BorisError(f'{stage} timed out after {timeout:g}s; see {self.state["log"]}')
                if time.monotonic() - heartbeat >= 30:
                    silence = int(time.monotonic() - self.last_activity)
                    self.event('running', f'{stage}: {int(elapsed)}s elapsed; last output {silence}s ago.')
                    heartbeat = time.monotonic()
            if buffer:
                failed |= self.output(buffer.decode(errors='replace') + '\n')
            code = self.child.wait(timeout=max(1, timeout - (time.monotonic() - started)))
            if code or failed:
                detail = self.state.get('agent_error') or re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', self.output_tail).strip()
                raise BorisError(f'{stage} failed (exit {code}){": " + str(detail) if detail else ""}; see {self.state["log"]}')
        except subprocess.TimeoutExpired:
            raise BorisError(f'{stage} timed out; see {self.state["log"]}')
        finally:
            selector.close()
            self.terminate_child()
            self.child.stdout.close()

    def execute(self):
        self.state['pid'] = os.getpid()
        (self.directory / 'boris.pid').write_text(str(os.getpid()) + '\n')
        self.event('starting', f"Boris started {self.args.mode} for {self.args.feature} using {self.state['agent']} / {self.state['model']}.")
        queue = tickets(self.directory)
        if all(t['done'] for t in queue):
            self.event('complete', 'All tickets are already complete.', exit_code=0)
            return 0
        if not frontier(queue):
            self.event('blocked', 'No runnable ticket; unfinished tickets need triage or completed blockers.', exit_code=1)
            return 1
        if self.args.mode == 'afk':
            self.event('sandbox', f"Starting sandbox {self.state['sandbox']}.")
            # Create/restart without attaching an interactive agent. Then exec explicit argv, without a PTY.
            self.command(['sbx', 'run', '--detached', '--name', self.state['sandbox'],
                          self.state['agent'], str(self.root)], self.args.startup_timeout, 'Sandbox startup')
        cap = 1 if self.args.mode == 'once' else self.args.iterations
        for iteration in range(1, cap + 1):
            queue = tickets(self.directory)
            ticket = frontier(queue)
            if not ticket:
                complete = all(t['done'] for t in queue)
                self.event('complete' if complete else 'blocked',
                           'All tickets complete.' if complete else 'No runnable ticket; remaining tickets are blocked or need triage.',
                           exit_code=0 if complete else 1, ticket=None)
                return 0 if complete else 1
            done_before = {t['id'] for t in queue if t['done']}
            progress = self.directory / 'progress.txt'
            old_progress = progress.read_bytes() if progress.exists() else b''
            self.state.pop('agent_summary', None)
            self.state.pop('agent_error', None)
            position = next(index for index, item in enumerate(queue, 1) if item['id'] == ticket['id'])
            self.event('ticket', f"Ticket {position}/{len(queue)}: Implementing {ticket['title']}.",
                       iteration=iteration, iteration_limit=cap, ticket=ticket['path'].name,
                       ticket_number=ticket['id'], ticket_position=position,
                       completed=len(done_before), total=len(queue))
            cmd = agent_command(self.state['agent'], self.state['model'], prompt_for(self.directory, ticket, len(done_before)), self.args.mode == 'afk')
            if self.args.mode == 'afk':
                cmd = ['sbx', 'exec', '--workdir', str(self.root), self.state['sandbox'], *cmd]
            self.command(cmd, self.args.ticket_timeout, f"Ticket {ticket['id']}")
            after = tickets(self.directory)
            done_after = {t['id'] for t in after if t['done']}
            if done_after - done_before != {ticket['id']} or not done_before <= done_after:
                raise BorisError(f"Ticket {ticket['id']} did not finish exactly one ticket with all criteria checked; stopped for review")
            if not progress.exists() or progress.read_bytes() == old_progress:
                raise BorisError(f"Ticket {ticket['id']} did not update progress.txt; stopped for review")
            self.event('ticket_done', f"Completed {ticket['title']} ({len(done_after)}/{len(after)} tickets).",
                       completed=len(done_after), total=len(after))
            if all(t['done'] for t in after):
                self.event('complete', f'All {len(after)} tickets complete.', exit_code=0, ticket=None)
                return 0
        if self.args.mode == 'once':
            self.event('finished', 'One ticket completed; once run finished.', exit_code=0, ticket=None)
            return 0
        self.event('limit', f'Iteration limit ({cap}) reached; unfinished tickets remain.', exit_code=1, ticket=None)
        return 1


def owns_lock(root, state):
    if not lock_busy(root):
        return False
    owner = (root / '.scratch' / '.boris.lock').read_text()
    return f'run={state.get("run_id")}' in owner and f'pid={state.get("pid")}' in owner


def write_owner(lock, feature, pid, run_id):
    lock.seek(0)
    lock.truncate()
    lock.write(f'{feature} pid={pid} run={run_id}\n')
    lock.flush()


def current_state(root, directory):
    state = read_json(directory / 'boris.status.json')
    if state and state.get('phase') not in TERMINAL and not owns_lock(root, state):
        state.update(phase='interrupted', message='Worker disappeared before recording an exit.', exit_code=1)
    return state


def report(root, directory, args):
    state = current_state(root, directory)
    if args.mode in {'status', 'progress'}:
        queue = tickets(directory)
        done = sum(t['done'] for t in queue)
        next_ticket = frontier(queue)
        state = dict(state, completed=done, total=len(queue), percent=(done * 100 + len(queue) // 2) // len(queue),
                     next=next_ticket['title'] if next_ticket else None,
                     tickets=[{k: v for k, v in t.items() if k != 'path'} for t in queue])
        if args.json:
            print(json.dumps(state))
        else:
            print(f'{state.get("phase", "idle")}: {state.get("message", "No run started.")}')
            print(f'{state["percent"]}% complete ({done}/{len(queue)})')
            print('Next: ' + (state['next'] or 'none runnable'))
            if state.get('log'):
                print('Log: ' + state['log'])
        return 0
    if not state:
        raise BorisError('no Boris run recorded for this feature')
    if args.mode == 'stop':
        if state['phase'] in TERMINAL:
            print(state['message'])
            return 0
        # Verify identity via the pidfile; the signal handler records the outcome.
        pid = state.get('pid')
        if not pid or not owns_lock(root, state):
            raise BorisError('worker is not running')
        os.kill(pid, signal.SIGTERM)
        print('Stop requested; worker will terminate the agent and record its exit.')
        return 0
    if args.verbose:
        # Diagnostic log following uses a separate byte cursor.
        offset_path = directory / 'boris.offset'
        offset = int(offset_path.read_text()) if offset_path.exists() else 0
        with open(state['log'], 'rb') as handle:
            if offset > os.fstat(handle.fileno()).st_size:
                offset = 0
            handle.seek(offset)
            data = handle.read()
            offset = handle.tell()  # Never advance past the bytes actually read.
        print(data.decode(errors='replace'), end='')
        offset_path.write_text(str(offset))
        if state['phase'] in TERMINAL:
            print(f'boris: exited ({state.get("exit_code", 1)})')
        return 0
    cursor = directory / 'boris.follow.json'
    saved = read_json(cursor) if args.since is None else {}
    since = args.since if args.since is not None else (saved.get('sequence', 0) if saved.get('run_id') == state['run_id'] else 0)
    deadline = time.monotonic() + args.wait
    while True:
        with open(state['events']) as handle:
            events = []
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue  # A concurrent append may not have finished its line yet.
                if event['sequence'] > since:
                    events.append(event)
        state = current_state(root, directory)
        if events or state['phase'] in TERMINAL or time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    sequence = events[-1]['sequence'] if events else since
    if args.since is None:
        atomic_json(cursor, dict(run_id=state['run_id'], sequence=sequence))
    if args.json:
        print(json.dumps(dict(run_id=state['run_id'], sequence=sequence, events=events, status=state,
                              exited=state['phase'] in TERMINAL)))
    else:
        for event in events:
            print(event['message'])
        if state['phase'] in TERMINAL:
            print(f'boris: exited ({state.get("exit_code", 1)})')
    return 0


def parser():
    p = argparse.ArgumentParser(prog='boris', description='Implement .scratch tickets sequentially, locally or in Docker Sandboxes.')
    p.add_argument('feature')
    p.add_argument('mode', nargs='?', default='once', choices=['once', 'afk', 'follow', 'status', 'progress', 'stop'])
    p.add_argument('iterations', nargs='?', type=int, default=20)
    p.add_argument('--agent', choices=['codex', 'claude', 'cursor'])
    p.add_argument('--model')
    p.add_argument('--foreground', action='store_true', help='stay attached and print milestones')
    p.add_argument('--verbose', action='store_true', help='also print detailed agent output')
    p.add_argument('--json', action='store_true', help='machine-readable status/follow output')
    p.add_argument('--since', type=int, help='follow events after this sequence; avoids a shared cursor')
    p.add_argument('--wait', type=float, default=0, help='follow long-poll timeout in seconds (max 30)')
    p.add_argument('--startup-timeout', type=float, default=180)
    p.add_argument('--ticket-timeout', type=float, default=3600)
    p.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    p.add_argument('--lock-fd', type=int, help=argparse.SUPPRESS)
    return p


def main():
    args = parser().parse_args()
    if args.iterations < 1 or args.startup_timeout <= 0 or args.ticket_timeout <= 0 or not 0 <= args.wait <= 30 or (args.since is not None and args.since < 0):
        raise BorisError('iterations/timeouts must be positive; wait must be 0–30; since must be nonnegative')
    root, directory = locate(args.feature)
    if args.mode not in {'once', 'afk'}:
        return report(root, directory, args)
    if args.worker:
        lock = os.fdopen(args.lock_fd, 'a+')
        state = read_json(directory / 'boris.status.json')
    else:
        lock = acquire(root)
        try:
            queue = tickets(directory)
            if not frontier(queue):
                if all(t['done'] for t in queue):
                    print('All tickets are already complete.')
                    return 0
                raise BorisError('No runnable ticket; unfinished tickets are blocked or need triage.')
            agent, model = resolve(args)
            preflight(root, args, agent)
            run_id = dt.datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8]
            run_dir = directory / '.boris' / run_id
            run_dir.mkdir(parents=True)
            state = dict(run_id=run_id, feature=args.feature, mode=args.mode, agent=agent, model=model,
                         phase='starting', message='Starting worker.', started_at=now(), sequence=0, pid=os.getpid(),
                         log=str(run_dir / 'agent.log'), events=str(run_dir / 'events.jsonl'),
                         sandbox=sandbox_name(root, agent) if args.mode == 'afk' else None)
            Path(state['log']).touch()
            Path(state['events']).touch()
            # Keep the familiar log path and preserve old logs rather than truncating them.
            alias = directory / 'boris.log'
            if alias.exists() and not alias.is_symlink():
                alias.rename(run_dir / 'previous.log')
            alias.unlink(missing_ok=True)
            alias.symlink_to(Path(state['log']))
            (directory / 'boris.offset').unlink(missing_ok=True)
            write_owner(lock, args.feature, os.getpid(), run_id)
            atomic_json(directory / 'boris.status.json', state)
            if not args.foreground:
                command = [sys.executable, str(Path(__file__).resolve()), args.feature, args.mode, str(args.iterations),
                           '--agent', agent, '--model', model, '--worker', '--lock-fd', str(lock.fileno()),
                           '--startup-timeout', str(args.startup_timeout), '--ticket-timeout', str(args.ticket_timeout)]
                # Pass the locked descriptor: no unlocked gap between launcher and worker.
                with open(run_dir / 'worker.log', 'a') as output:
                    child = subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                             pass_fds=(lock.fileno(),), start_new_session=True)
                state['pid'] = child.pid
                write_owner(lock, args.feature, child.pid, run_id)
                atomic_json(directory / 'boris.status.json', state)
                # The worker is released only after its parent has published the PID.
                (run_dir / 'launch.ready').touch()
                print(f"Boris started: {args.mode}, {agent} / {model}, pid {child.pid}\nLog: {state['log']}\nStatus: boris {args.feature} status")
                return 0
        finally:
            if not args.foreground:
                lock.close()
    run = Run(root, directory, args, state)
    if args.worker:
        ready = Path(state['log']).parent / 'launch.ready'
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        if not ready.exists():
            run.event('failed', 'Launcher disappeared before publishing worker PID.', exit_code=1)
            lock.close()
            return 1
    write_owner(lock, args.feature, os.getpid(), state['run_id'])

    def stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        return run.execute()
    except KeyboardInterrupt:
        run.terminate_child()
        run.event('stopped', 'Boris stopped; unfinished work remains available for review.', exit_code=130)
        return 130
    except Exception as exc:
        run.terminate_child()
        run.event('failed', str(exc), exit_code=1)
        return 1
    finally:
        run.log.close()
        run.events.close()
        (directory / 'boris.pid').unlink(missing_ok=True)
        lock.close()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (BorisError, OSError, ValueError) as exc:
        print(f'boris: {exc}', file=sys.stderr)
        sys.exit(1)
