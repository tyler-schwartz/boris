import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
BORIS = ROOT / 'boris'
sys.path.insert(0, str(ROOT / 'lib'))
spec = importlib.util.spec_from_file_location('boris_runtime', ROOT / 'lib/boris.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)

# A real subprocess stand-in for agent CLIs and sandboxd. No credentials or network.
FAKE = r'''#!/usr/bin/env python3
import json, os, pathlib, re, sys, time
args = sys.argv[1:]
kind = pathlib.Path(sys.argv[0]).name
behavior = os.environ.get('FAKE_BEHAVIOR', '')
if kind == 'sbx':
    if args[:3] == ['secret', 'ls', '--json']:
        names = [] if behavior == 'missing_auth' else ['openai', 'cursor', 'anthropic']
        print(json.dumps({'secrets': [{'name': n, 'scope': 'global'} for n in names]})); sys.exit()
    if args[0] == 'ls':
        print('{"sandboxes": []}'); sys.exit()
    if args[0] == 'stop':
        pathlib.Path('sandbox-stopped').touch(); sys.exit()
    if args[0] == 'run':
        with open('launches', 'a') as f: f.write(json.dumps(args) + '\n')
        if behavior == 'startup_fail': print('VM failed to boot', flush=True); sys.exit(9)
        if behavior == 'startup_hang': time.sleep(10)
        print('Sandbox ready'); sys.exit()
    if args[0] == 'exec':
        args = args[4:] # exec --workdir ROOT NAME AGENT ...
        kind, args = args[0], args[1:]
if kind == 'agent' and args == ['models']:
    print('thread-model - Thread Model\nexact - Exact\ncursor-grok-4.6-high - Grok 4.6\ncursor-grok-4.6-low - Grok 4.6 Low\ncursor-grok-4.6-high-fast - Grok 4.6 Fast'); sys.exit()
prompt = args[-1]
path = pathlib.Path(re.search(r'ONE ticket: (.+?) \(', prompt)[1])
with open('calls', 'a') as f: f.write(json.dumps({'ticket': path.name, 'kind':kind, 'args':args}) + '\n')
active = pathlib.Path('active')
if active.exists(): print('OVERLAP'); sys.exit(88)
active.touch()
try:
    print(json.dumps({'type':'item.started','item':{'type':'command_execution','command':'run checks'}}), flush=True)
    if behavior == 'hang': time.sleep(10)
    time.sleep(.05)
    if behavior == 'error_zero':
        print(json.dumps({'type':'result','is_error':True,'result':'permission denied'})); sys.exit()
    if behavior == 'marker_only':
        print('<promise>COMPLETE</promise>'); sys.exit()
    text = path.read_text().replace('ready-for-agent', 'done').replace('[ ]', '[x]')
    path.write_text(text)
    if behavior == 'two_tickets':
        for other in path.parent.glob('*.md'):
            other.write_text(other.read_text().replace('ready-for-agent', 'done').replace('[ ]', '[x]'))
    if behavior != 'no_progress':
        with open(path.parent.parent / 'progress.txt', 'a') as f: f.write(path.name + ' verified\n')
    print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Implemented and checked '+path.name}}),flush=True)
finally:
    active.unlink(missing_ok=True)
'''


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='boris-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        for name in ['codex', 'claude', 'agent', 'sbx']:
            p = self.bin / name
            p.write_text(FAKE)
            p.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'])
        for name in ['BORIS_AGENT', 'BORIS_MODEL', 'T3CODE_THREAD_ID', 'T3CODE_PROJECT_ROOT', 'CLAUDE_CODE_SESSION_ID', 'CURSOR_CONVERSATION_ID']:
            self.env.pop(name, None)
        self.feature('demo')

    def feature(self, name, count=3):
        d = self.root / '.scratch' / name
        (d / 'issues').mkdir(parents=True)
        (d / 'spec.md').write_text('Implement all slices.\n')
        for i in range(1, count+1):
            blocker = 'None (can start immediately)' if i == 1 else f'{i-1:02} Previous slice'
            (d / 'issues' / f'{i:02}-slice.md').write_text(f'# {i:02}: Slice {i}\n\n**Blocked by:** {blocker}\n\n**Status:** ready-for-agent\n\n- [ ] Verified behavior\n')
        return d

    def cli(self, *args, check=True):
        result = subprocess.run([str(BORIS), *args], cwd=self.root, env=self.env, capture_output=True, text=True, timeout=10)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return result

    def state(self, feature='demo'):
        return json.loads((self.root / '.scratch' / feature / 'boris.status.json').read_text())

    def wait(self, phase=None, feature='demo'):
        deadline = time.monotonic()+5
        while time.monotonic() < deadline:
            state = self.state(feature)
            if state['phase'] == phase or (phase is None and state['phase'] in runtime.TERMINAL):
                # Terminal event is written before the worker releases the lock.
                if phase is None:
                    while runtime.lock_busy(self.root) and time.monotonic() < deadline:
                        time.sleep(.01)
                return state
            time.sleep(.02)
        self.fail(f'worker did not reach {phase}: {self.state(feature)}')

    def launch(self, mode='once', *extra, agent='codex'):
        return self.cli('demo', mode, '--agent', agent, '--model', 'thread-model', *extra)

    def calls(self):
        return [json.loads(line) for line in (self.root / 'calls').read_text().splitlines()]

    def test_once_completes_only_one_and_reports_after_exit(self):
        self.launch()
        self.assertEqual(self.wait()['phase'], 'finished')
        self.assertEqual(len(self.calls()), 1)
        report = json.loads(self.cli('demo','status','--json').stdout)
        self.assertEqual(report['completed'], 1)
        events = json.loads(self.cli('demo','follow','--json','--since','0').stdout)
        self.assertTrue(events['exited'])
        self.assertIn('ticket_done', [e['type'] for e in events['events']])
        self.assertIn('Implemented', events['status']['agent_summary'])
        self.assertEqual(json.loads(self.cli('demo','follow','--json','--since',str(events['sequence'])).stdout)['events'], [])

    def test_afk_runs_in_order_in_one_named_sandbox_without_tty(self):
        self.launch('afk')
        self.assertEqual(self.wait()['phase'], 'complete')
        self.assertEqual([c['ticket'] for c in self.calls()], ['01-slice.md','02-slice.md','03-slice.md'])
        launches = (self.root / 'launches').read_text().splitlines()
        self.assertEqual(len(launches), 1)
        self.assertIn('--detached', json.loads(launches[0]))
        self.assertEqual(self.state()['completed'], 3)

    def test_all_providers_receive_exact_model(self):
        for agent in ['cursor','claude','codex']:
            self.feature(agent, 1)
            self.cli(agent,'once','--foreground','--agent',agent,'--model','exact[effort=high]')
            state = self.state(agent)
            self.assertEqual(state['phase'], 'complete')
            self.assertEqual(state['model'], 'exact[effort=high]')
            self.assertIn('exact[effort=high]', self.calls()[-1]['args'])

    def test_blocked_queue_does_not_launch_or_claim_complete(self):
        p = self.root / '.scratch/demo/issues/01-slice.md'
        p.write_text(p.read_text().replace('ready-for-agent','needs-triage'))
        result = self.launch_result('afk')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('blocked', result.stderr)
        self.assertFalse((self.root/'calls').exists())

    def launch_result(self, mode='once', *extra):
        return self.cli('demo',mode,'--agent','codex','--model','thread-model',*extra,check=False)

    def test_model_promise_does_not_complete_queue(self):
        self.env['FAKE_BEHAVIOR'] = 'marker_only'
        self.launch('afk')
        self.assertEqual(self.wait()['phase'], 'failed')
        self.assertEqual(len(self.calls()), 1)

    def test_missing_progress_and_multiple_completions_stop_loop(self):
        for behavior in ['no_progress','two_tickets']:
            with self.subTest(behavior=behavior):
                self.env['FAKE_BEHAVIOR'] = behavior
                self.feature(behavior)
                self.cli(behavior,'afk','--foreground','--agent','codex','--model','thread-model',check=False)
                self.assertEqual(self.state(behavior)['phase'], 'failed')

    def test_cap_is_distinct_from_complete(self):
        self.launch('afk','1')
        self.assertEqual(self.wait()['phase'], 'limit')
        self.assertEqual(len(self.calls()), 1)

    def test_workspace_lock_blocks_other_features(self):
        self.env['FAKE_BEHAVIOR'] = 'hang'
        self.feature('other')
        self.launch()
        self.wait('ticket')
        result = self.cli('other','once','--agent','codex','--model','thread-model',check=False)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('already owns',result.stderr)
        self.cli('demo','stop')
        self.assertEqual(self.wait()['phase'],'stopped')
        self.assertFalse(runtime.lock_busy(self.root))

    def test_timeout_stops_sandbox_and_releases_lock(self):
        self.env['FAKE_BEHAVIOR'] = 'hang'
        self.launch('afk','--ticket-timeout','.2')
        state = self.wait()
        self.assertEqual(state['phase'],'failed')
        self.assertIn('timed out',state['message'])
        self.assertTrue((self.root/'sandbox-stopped').exists())
        self.assertFalse(runtime.lock_busy(self.root))

    def test_startup_error_keeps_raw_error(self):
        self.env['FAKE_BEHAVIOR'] = 'startup_fail'
        self.launch('afk')
        state = self.wait()
        self.assertEqual(state['phase'],'failed')
        self.assertIn('VM failed to boot',Path(state['log']).read_text())
        self.assertIn('VM failed to boot',state['message'])
        self.assertFalse((self.root/'calls').exists())

    def test_missing_auth_fails_before_detaching(self):
        self.env['FAKE_BEHAVIOR'] = 'missing_auth'
        result = self.launch_result('afk')
        self.assertIn('sbx secret set openai --oauth',result.stderr)
        self.assertFalse((self.root/'.scratch/demo/boris.status.json').exists())

    def test_zero_exit_with_agent_error_is_failure(self):
        self.env['FAKE_BEHAVIOR'] = 'error_zero'
        self.launch('afk')
        state = self.wait()
        self.assertEqual(state['phase'],'failed')
        self.assertIn('permission denied',state['message'])
        self.assertEqual(len(self.calls()),1)

    def test_history_survives_relaunch_and_follow_resets(self):
        self.launch()
        first = self.wait()
        self.cli('demo','follow')
        self.launch()
        second = self.wait()
        self.assertNotEqual(first['run_id'],second['run_id'])
        self.assertTrue(Path(first['log']).exists())
        self.assertIn('Slice 2',self.cli('demo','follow').stdout)

    def test_stale_worker_status_is_interrupted(self):
        self.launch()
        self.wait()
        state = self.state()
        state['phase']='running'
        runtime.atomic_json(self.root/'.scratch/demo/boris.status.json',state)
        report = json.loads(self.cli('demo','status','--json').stdout)
        self.assertEqual(report['phase'],'interrupted')

    def test_queue_handles_real_ticket_formats_and_legacy_resolved(self):
        d = self.root/'.scratch/demo'
        p = d/'issues/01-slice.md'
        p.write_text(p.read_text().replace('ready-for-agent','resolved').replace('[ ]','[x]'))
        self.assertEqual(runtime.frontier(runtime.tickets(d))['id'],2)
        p.write_text(p.read_text().replace('[x]','[ ]'))
        self.assertIsNone(runtime.frontier(runtime.tickets(d)))
        p.write_text(p.read_text().replace('None (can start immediately)','99 Missing'))
        with self.assertRaises(runtime.BorisError): runtime.tickets(d)

    def test_explicit_agent_and_model_bypass_broken_session_discovery(self):
        self.env['T3CODE_THREAD_ID']='nonexistent-thread'
        self.launch()
        self.assertEqual(self.wait()['phase'],'finished')

    def test_each_completed_ticket_keeps_its_own_summary(self):
        self.launch('afk')
        self.wait()
        report = json.loads(self.cli('demo','follow','--json','--since','0').stdout)
        done = [e for e in report['events'] if e['type']=='ticket_done']
        self.assertEqual(len(done),3)
        for number, event in enumerate(done,1):
            self.assertIn(f'{number:02}-slice.md',event['summary'])

    def test_independent_followers_do_not_consume_each_others_events(self):
        self.launch()
        self.wait()
        first = json.loads(self.cli('demo','follow','--json','--since','0').stdout)
        second = json.loads(self.cli('demo','follow','--json','--since','0').stdout)
        self.assertEqual(first['events'],second['events'])

    def test_stop_afk_stops_its_dedicated_sandbox(self):
        self.env['FAKE_BEHAVIOR']='hang'
        self.launch('afk')
        self.wait('ticket')
        self.cli('demo','stop')
        self.assertEqual(self.wait()['phase'],'stopped')
        self.assertTrue((self.root/'sandbox-stopped').exists())

    def test_ticket_denominator_is_feature_total_even_with_smaller_cap(self):
        self.feature('four',4)
        self.cli('four','afk','1','--agent','codex','--model','thread-model')
        self.assertEqual(self.wait(feature='four')['phase'],'limit')
        report=json.loads(self.cli('four','follow','--json','--since','0').stdout)
        event=next(e for e in report['events'] if e['type']=='ticket')
        self.assertIn('Ticket 1/4',event['message'])
        self.assertEqual(event['total'],4)
        self.assertEqual(event['iteration_limit'],1)
        self.cli('four','once','--agent','codex','--model','thread-model')
        self.wait(feature='four')
        report=json.loads(self.cli('four','follow','--json','--since','0').stdout)
        event=next(e for e in report['events'] if e['type']=='ticket')
        self.assertIn('Ticket 2/4',event['message'])

    def test_cursor_ui_name_is_resolved_before_agent_launch(self):
        self.cli('demo','afk','1','--agent','cursor','--model','Cursor Grok 4.6')
        self.wait()
        self.assertEqual(self.state()['model'],'cursor-grok-4.6-high')
        self.assertIn('cursor-grok-4.6-high',self.calls()[0]['args'])
        self.assertNotIn('Cursor Grok 4.6',self.calls()[0]['args'])

    def test_unknown_cursor_model_fails_before_starting_sandbox(self):
        result=self.cli('demo','afk','--agent','cursor','--model','Unknown Model',check=False)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('unknown Cursor model',result.stderr)
        self.assertFalse((self.root/'launches').exists())
        self.assertFalse((self.root/'.scratch/demo/boris.status.json').exists())

    def test_feature_traversal_is_rejected(self):
        result = self.cli('../demo','once',check=False)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('single .scratch directory',result.stderr)


if __name__ == '__main__':
    unittest.main()
