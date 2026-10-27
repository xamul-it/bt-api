import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from app.service.replay_service import ReplayService, ReplayConflict


def test_replay_lifecycle_only_controls_its_child_and_does_not_inherit_broker_secrets(tmp_path, monkeypatch):
    from app.service import replay_service as module
    monkeypatch.setenv('ALPACA_SECRET_KEY', 'must-not-reach-replay')
    process = Mock()
    process.poll.return_value = None
    captured = []
    def popen(command, **kwargs):
        captured.append((command, kwargs))
        return process
    monkeypatch.setattr(module.subprocess, 'Popen', popen)
    service = ReplayService(tmp_path)
    state = service.prepare(['AAPL'], '2026-07-14', 60, 'ah', tmp_path)
    assert state['state'] == 'PREPARING'
    assert state['running'] is True
    assert 'ALPACA_SECRET_KEY' not in captured[0][1]['env']
    command = captured[0][0]
    assert command[command.index('--req-port') + 1] == '5557'
    assert command[command.index('--pub-port') + 1] == '5558'
    assert '--parent-pid' in command
    with pytest.raises(ReplayConflict):
        service.prepare(['MSFT'], '2026-07-14', 60, 'ah', tmp_path)
    service.status_file.write_text(json.dumps({'state': 'IDLE', 'available': {'AAPL': {'quote': 1}}}))
    assert service.status()['state'] == 'IDLE'
    def wait(**kwargs):
        process.poll.return_value = 0
    process.wait.side_effect = wait
    service.stop()
    process.terminate.assert_called_once()
    process.kill.assert_not_called()
    assert service.status()['running'] is False


def test_replay_rejects_unsupported_control_before_opening_sockets(tmp_path):
    service = ReplayService(tmp_path)
    for action in [None, [], {}, 'delete_files']:
        with pytest.raises(ValueError):
            service.control(action)
