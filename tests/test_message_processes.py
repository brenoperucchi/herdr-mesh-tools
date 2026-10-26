"""CLI/worker subprocess harness. It cannot reach a production Herdr socket."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from _herdr_message_ledger import Ledger
from _herdr_message_worker import healthy
from test_message_ledger import endpoint

ROOT = Path(__file__).resolve().parents[1]


def write_control(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


@pytest.fixture
def harness(tmp_path):
    control = tmp_path/'control.json'
    calls = tmp_path/'calls.jsonl'
    write_control(control, {'busy': True, 'ambiguous': False})
    fake = tmp_path/'fake-herdr'
    fake.write_text('''#!/usr/bin/env python3
import json,os,sys
with open(os.environ['TEST_CALLS'], 'a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')
control=json.load(open(os.environ['TEST_CONTROL']))
if control['ambiguous']:
 print('receipt lost')
else:
 name='a' if sys.argv[3].endswith('pa') else 'b'
 e=dict(name=name,agent='codex',pane_id='w1:p'+name,workspace_id='w1',terminal_id='term-'+name,cwd='/tmp/project',agent_session=dict(kind='id',source='herdr:codex',value='session-'+name))
 print(json.dumps(dict(result=dict(type='agent_prompted',agent=e))))
''')
    fake.chmod(0o700)
    wrapper = tmp_path/'entry.py'
    wrapper.write_text('''import contextlib,json,os,runpy,sys
from pathlib import Path
sys.path.insert(0, os.environ['TEST_BIN'])
import _herdr_message_runtime as runtime
from _herdr_message_ledger import endpoint
class Fake(runtime.Runtime):
 def __init__(self, route=None):
  super().__init__(env={**os.environ,'HERDR_SOCKET_PATH':'/test/never-connect'},route=route)
  self.binary=os.environ['TEST_HERDR']
 def caller(self):
  return self.get('w1:p'+os.environ.get('TEST_CALLER','a'))
 def get(self,pane):
  name='a' if pane.endswith('pa') else 'b'
  c=json.load(open(os.environ['TEST_CONTROL']))
  return dict(name=name,agent='codex',pane_id='w1:p'+name,workspace_id='w1',terminal_id='term-'+name,cwd='/tmp/project',agent_session=dict(kind='id',source='herdr:codex',value='session-'+name),agent_status='working' if name=='a' and c['busy'] else 'idle')
 def guard(self,info):
  return (info['agent_status']=='idle', 'origin_busy' if info['agent_status']=='working' else None)
 @contextlib.contextmanager
 def reservation(self,info):
  yield
runtime.Runtime=Fake
runpy.run_path(str(Path(os.environ['TEST_BIN'])/'herdr-message'),run_name='__main__')
''')
    env = {**os.environ, 'TEST_BIN': str(ROOT/'bin'), 'TEST_HERDR': str(fake),
           'TEST_CONTROL': str(control), 'TEST_CALLS': str(calls)}
    # Runtime constructor in the wrapper fixes a fake socket; all reads are overridden.
    state = tmp_path/'state'
    with Ledger(state, __import__('_herdr_message_runtime').Runtime(env={'HERDR_SOCKET_PATH':'/test/never-connect'}).scope) as db:
        scope = db.meta('scope')
    def command(*args, caller='a'):
        return [sys.executable, str(wrapper), '--state-dir', str(state), *args], {**env, 'TEST_CALLER': caller}
    def invoke(*args, caller='a'):
        argv, childenv = command(*args, caller=caller)
        r = subprocess.run(argv, env=childenv, capture_output=True, text=True, timeout=5)
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)
    def invoke_wire(argv, *, caller='b'):
        # Replace only executable with fake-transport driver. No hidden state/route flags.
        childenv = {**env, 'TEST_CALLER': caller, 'HERDR_CONFIG_PATH': '/different/caller/config'}
        r = subprocess.run([sys.executable, str(wrapper), *argv[1:]], env=childenv,
                           capture_output=True, text=True, timeout=5)
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)
    invoke.wire = invoke_wire
    workers = []
    def start():
        argv, childenv = command('worker', '--interval', '0.1')
        p = subprocess.Popen(argv, env=childenv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        workers.append(p)
        return p
    def until(predicate):
        end = time.monotonic()+5
        while time.monotonic()<end:
            if predicate():
                return
            time.sleep(0.01)
        pytest.fail('process harness deadline')
    try:
        yield state, scope, control, calls, invoke, start, until
    finally:
        for p in workers:
            if p.poll() is None:
                p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()  # test-owned fake worker only; no agent processes exist in harness.
                p.wait()


def test_cli_worker_ryzen9_and_exclusive_supervision(harness, tmp_path):
    state, scope, control, calls, invoke, start, until = harness
    p = start()
    with Ledger(state, scope) as db:
        until(lambda: healthy(db))
        second = start()
        second.wait(timeout=3)
        assert second.returncode != 0
        body = tmp_path/'request'
        body.write_text('Build ryzen9')
        req = invoke('send', 'b', '--subject', 'Build', '--key', 'build', '--file', str(body))
        assert req['code'] == 'queued'
        until(lambda: db.get(req['id'])['transport'] == 'submitted')
        answer = tmp_path/'answer'
        answer.write_text('Build complete')
        reply = invoke('reply', req['id'], '--key', 'result', '--file', str(answer), caller='b')
        assert reply['code'] == 'reply_recorded'
        until(lambda: db.status(req['id'])['state'] == 'return_pending_origin_busy')
        assert len(calls.read_text().splitlines()) == 1
        write_control(control, {'busy': False, 'ambiguous': False})
        until(lambda: db.status(req['id'])['state'] == 'reply_submitted')
        assert len(calls.read_text().splitlines()) == 2
        # Replay after delivery stays idempotent even across independent CLI processes.
        again = invoke('reply', req['id'], '--key', 'result', '--file', str(answer), caller='b')
        assert again['id'] == reply['id']
        assert db.get(req['id'])['response'] == 'reply_submitted'
        assert len(calls.read_text().splitlines()) == 2


def test_process_receipt_loss_survives_worker_restart_without_task_retry(harness, tmp_path):
    state, scope, control, calls, invoke, start, until = harness
    write_control(control, {'busy': True, 'ambiguous': True})
    p = start()
    with Ledger(state, scope) as db:
        until(lambda: healthy(db))
        body = tmp_path/'request'
        body.write_text('ONE_TASK')
        req = invoke('send', 'b', '--subject', 'Work', '--key', 'once', '--file', str(body))
        until(lambda: db.get(req['id'])['transport'] == 'unknown')
        p.terminate()
        p.wait(timeout=4)
        start()
        until(lambda: healthy(db))
        sent = [json.loads(line) for line in calls.read_text().splitlines()]
        assert sum('ONE_TASK' in args[-1] for args in sent) == 1
        assert db.get(req['id'])['transport'] == 'unknown'


def test_generated_reply_command_works_without_hidden_harness_flags(harness, tmp_path):
    import shlex
    state, scope, control, calls, invoke, start, until = harness
    start()
    with Ledger(state, scope) as db:
        until(lambda: healthy(db))
        body = tmp_path/'request'
        body.write_text('Build')
        req = invoke('send', 'b', '--subject', 'Build', '--key', 'build', '--file', str(body))
        until(lambda: db.get(req['id'])['transport'] == 'submitted')
        wire = json.loads(calls.read_text().splitlines()[0])[-1]
        line = next(line for line in wire.splitlines() if line.startswith('Reply-Command: '))
        argv = shlex.split(line.removeprefix('Reply-Command: '))
        answer = tmp_path/'reply'
        answer.write_text('Ready')
        argv[argv.index('--file')+1] = str(answer)
        result = invoke.wire(argv)
        assert result['code'] == 'reply_recorded'
        assert db.status(req['id'])['reply']['id'] == result['id']
