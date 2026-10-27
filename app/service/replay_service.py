"""One isolated replay subprocess; never starts strategies or contacts a broker."""
import json
import os
from pathlib import Path
import subprocess
import threading
import uuid

import zmq

from app.paths import WORKSPACE_ROOT


class ReplayConflict(ValueError):
    pass


class ReplayService:
    def __init__(self, runtime_dir=None):
        self.runtime_dir = Path(runtime_dir or WORKSPACE_ROOT / 'bt-api/config/replay')
        self.lock = threading.RLock()
        self.process = None
        self.status_file = None
        self.log_file = None
        self.session_id = None
        self.requested_symbols = []

    def status(self):
        with self.lock:
            if self.process is None:
                return {'state': 'STOPPED', 'managed': False}
            state = {'state': 'PREPARING'}
            if self.status_file and self.status_file.exists():
                try:
                    state = json.loads(self.status_file.read_text())
                except (OSError, ValueError):
                    pass
            code = self.process.poll()
            if code is not None and state.get('state') not in ('ERROR', 'STOPPED'):
                state['state'] = 'STOPPED' if code == 0 else 'ERROR'
                state['error'] = 'Il processo replay è terminato' if code else None
            if code is not None and state.get('state') == 'ERROR' and not state.get('error'):
                state['error'] = 'Impossibile avviare il replay: verifica disponibilità porte e archivi'
            return {**state, 'managed': True, 'running': code is None,
                    'session_id': self.session_id, 'requested_symbols': self.requested_symbols}

    def prepare(self, symbols, replay_date, speed, session, log_dir, remap_today=False):
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                raise ReplayConflict('Un replay è già aperto: arrestalo prima di preparare una nuova sessione')
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            self.session_id = uuid.uuid4().hex
            self.requested_symbols = symbols[:]
            self.status_file = self.runtime_dir / f'{self.session_id}.json'
            self.log_file = self.runtime_dir / f'{self.session_id}.log'
            command = [str(WORKSPACE_ROOT / 'bt-gateway/.venv/bin/python'),
                       str(WORKSPACE_ROOT / 'bt-gateway/bt_alpaca_zmq/replay_zmq_proxy.py'),
                       '--date', replay_date, '--symbols', ','.join(symbols),
                       '--speed', str(speed), '--session', session, '--log-dir', str(log_dir),
                       '--req-port', '5557', '--pub-port', '5558',
                       '--status-file', str(self.status_file), '--parent-pid', str(os.getpid())]
            if remap_today:
                command.append('--remap-today')
            with self.log_file.open('ab') as handle:
                self.process = subprocess.Popen(command, cwd=WORKSPACE_ROOT, stdout=handle,
                                                stderr=subprocess.STDOUT, close_fds=True,
                                                env={'PATH': os.defpath, 'LANG': 'C.UTF-8', 'PYTHONUNBUFFERED': '1'})
            return self.status()

    def control(self, action):
        commands = {'preview_start': 'PREVIEW:START', 'preview_stop': 'PREVIEW:STOP', 'reset': 'RESET'}
        if not isinstance(action, str) or action not in commands:
            raise ValueError('Azione replay non valida')
        with self.lock:
            if self.status().get('state') not in ('IDLE', 'REPLAYING', 'EOD_SIGNALED'):
                raise ReplayConflict('Il replay non è pronto')
            context = zmq.Context()
            socket = context.socket(zmq.REQ)
            socket.setsockopt(zmq.LINGER, 0)
            socket.setsockopt(zmq.RCVTIMEO, 1500)
            socket.setsockopt(zmq.SNDTIMEO, 1500)
            socket.connect('tcp://127.0.0.1:5557')
            try:
                socket.send_string(commands[action])
                reply = socket.recv().decode()
            except zmq.ZMQError:
                raise ReplayConflict('Il proxy replay non risponde') from None
            finally:
                socket.close(0)
                context.term()
            if reply.startswith('ERR:'):
                raise ReplayConflict(reply)
            return {'accepted': True, 'action': action}

    def stop(self):
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
            return self.status()


replay_service = ReplayService()
