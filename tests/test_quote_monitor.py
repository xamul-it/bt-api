import json
import time
from flask import Flask
import pytest
from app import quote_monitor as module


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(module, 'CONFIG', tmp_path / 'config.json')
    monkeypatch.setattr(module, 'STATUS', tmp_path / 'status.json')
    monkeypatch.setattr(module, 'DUMP', tmp_path)
    app = Flask(__name__)
    app.register_blueprint(module.quote_monitor_bp, url_prefix='/monitor')
    return app.test_client()


def test_config_persists_selection_and_deduplicates(client):
    assert client.get('/monitor/config').json == {'symbols': []}
    assert client.put('/monitor/config', json={'symbols': ['AAPL', 'BRK.B', 'AAPL']}).json['symbols'] == ['AAPL', 'BRK.B']
    assert client.get('/monitor/status').json['desired_symbols'] == ['AAPL', 'BRK.B']
    assert client.put('/monitor/config', json={'symbols': []}).status_code == 200


@pytest.mark.parametrize('symbols', [None, 'AAPL', ['../secret'], ['aapl'], [42], ['AAPL'] * 101])
def test_invalid_config_does_not_replace_selection(client, symbols):
    client.put('/monitor/config', json={'symbols': ['MSFT']})
    assert client.put('/monitor/config', json={'symbols': symbols}).status_code == 400
    assert client.get('/monitor/config').json['symbols'] == ['MSFT']


def test_status_does_not_treat_old_snapshot_as_running(client):
    module.STATUS.write_text(json.dumps({'updated_at': time.time() - 100, 'proxy_connected': True}))
    assert client.get('/monitor/status').json['logger_alive'] is False


def test_recording_tail_is_bounded_and_tolerates_partial_line(client):
    with (module.DUMP / 'AAPL.json').open('w') as handle:
        for i in range(1200):
            handle.write(json.dumps({'timestamp': i, 'padding': 'x' * 2000}) + '\n')
        handle.write('{incomplete')
    data = client.get('/monitor/recordings/AAPL').json
    assert data['truncated'] is True
    assert len(data['rows']) == 500
    assert data['rows'][-1]['timestamp'] == 1199
    assert data['rows'][0]['timestamp'] == 700


def test_history_requires_context_and_closed_session(client):
    assert client.get('/monitor/history/AAPL?date=2026-10-01').status_code == 400
    assert client.get('/monitor/history/AAPL?portfolio_key_id=p&date=2099-01-01').status_code == 400


def test_history_uses_sip_context_and_keeps_pagination(client, monkeypatch):
    from app import live
    import requests
    monkeypatch.setattr(live, '_resolve_portfolio_credentials', lambda key: ('chosen', 'private', True, key, 'Name'))
    class Response:
        status_code = 200
        def json(self):
            return {'quotes': {'AAPL': [{'t': '2026-10-01T20:00:00Z', 'bp': 100}]}, 'next_page_token': 'next'}
    seen = []
    def get(url, **kwargs):
        seen.append(kwargs)
        return Response()
    monkeypatch.setattr(requests, 'get', get)
    data = client.get('/monitor/history/AAPL?portfolio_key_id=p&date=2026-10-01&page_token=previous').json
    assert data['next_page_token'] == 'next'
    assert data['rows'][0]['bp'] == 100
    assert seen[0]['params']['feed'] == 'sip'
    assert seen[0]['params']['start'] == '2026-10-01T20:00:00+00:00'
    assert seen[0]['params']['end'] == '2026-10-02T00:00:00+00:00'
    assert seen[0]['params']['page_token'] == 'previous'
    assert seen[0]['headers']['APCA-API-KEY-ID'] == 'chosen'
    assert 'private' not in json.dumps(data)


def snapshot(clients=None):
    module.STATUS.write_text(json.dumps({'updated_at': time.time(), 'proxy_connected': True,
                                       'proxy': {'clients': clients or []}}))


def test_watchtower_and_manual_ownership_are_independent(client):
    client.put('/monitor/config', json={'symbols': ['AAPL']})
    assert client.put('/monitor/registrations/watchtower', json={'symbols': ['AAPL', 'MSFT']}).status_code == 200
    client.put('/monitor/config', json={'symbols': []})
    assert client.get('/monitor/status').json['desired_symbols'] == ['AAPL', 'MSFT']
    assert client.get('/monitor/registrations/watchtower').json['symbols'] == ['AAPL', 'MSFT']
    assert client.get('/monitor/config').json['symbols'] == []


def test_archives_pagination_retries_incomplete_line(client):
    path = module.DUMP / 'AAPL.json'
    path.write_text('{"timestamp":1}\n{"timestamp":2')
    data = client.get('/monitor/archives/AAPL/events').json
    assert data['rows'] == [{'timestamp': 1}]
    assert data['complete'] is False
    with path.open('a') as handle:
        handle.write('}\n')
    next_page = client.get(f"/monitor/archives/AAPL/events?offset={data['next_offset']}").json
    assert next_page['rows'] == [{'timestamp': 2}]
    assert next_page['complete'] is True


def test_archive_inventory_and_download_reject_symlinks(client, tmp_path):
    (module.DUMP / 'AAPL.json').write_text('{"price":1}\n')
    (module.DUMP / 'MSFT.json').symlink_to(module.DUMP / 'AAPL.json')
    assert [row['symbol'] for row in client.get('/monitor/archives').json['rows']] == ['AAPL']
    assert client.get('/monitor/archives/MSFT/download').status_code == 400
    assert client.get('/monitor/archives/AAPL/download').data == b'{"price":1}\n'


def test_delete_rejects_unconfirmed_active_and_changed_files(client):
    path = module.DUMP / 'AAPL.json'
    path.write_text('{}\n')
    version = module.archive_version(path)
    assert client.delete('/monitor/archives/AAPL', json={}).status_code == 400
    assert client.delete('/monitor/archives/AAPL', json={'confirm_symbol': 'AAPL', 'version': version}).status_code == 409
    snapshot([{'subscriptions': [{'symbol': 'AAPL', 'channel': 'quote'}]}])
    assert client.delete('/monitor/archives/AAPL', json={'confirm_symbol': 'AAPL', 'version': version}).status_code == 409
    snapshot()
    path.write_text('{"new":true}\n')
    assert client.delete('/monitor/archives/AAPL', json={'confirm_symbol': 'AAPL', 'version': version}).status_code == 409
    assert path.exists()
    assert client.delete('/monitor/archives/AAPL', json={'confirm_symbol': 'AAPL', 'version': module.archive_version(path)}).status_code == 200
    assert not path.exists()


def test_delete_protects_saved_watchtower_symbols(client):
    (module.DUMP / 'AAPL.json').write_text('{}\n')
    snapshot()
    client.put('/monitor/registrations/watchtower', json={'symbols': ['AAPL']})
    assert client.delete('/monitor/archives/AAPL', json={'confirm_symbol': 'AAPL', 'version': module.archive_version(module.DUMP / 'AAPL.json')}).status_code == 409


def test_live_cursor_reports_gaps_and_restart(client):
    module.STATUS.write_text(json.dumps({'session_id': 'new', 'sequence': 550,
        'events': [{'sequence': 549, 'symbol': 'AAPL'}, {'sequence': 550, 'symbol': 'MSFT'}]}))
    data = client.get('/monitor/live?after=1&session=new&symbol=AAPL').json
    assert data['gap'] is True
    assert data['sequence'] == 550
    assert data['rows'] == [{'sequence': 549, 'symbol': 'AAPL'}]
    data = client.get('/monitor/live?after=999&session=old').json
    assert data['reset'] is True
    assert len(data['rows']) == 2
    assert 'events' not in client.get('/monitor/status').json


def test_replay_request_validates_inputs_and_passes_only_selected_archives(client, monkeypatch):
    from app.service.replay_service import replay_service
    calls = []
    monkeypatch.setattr(replay_service, 'prepare', lambda *args: calls.append(args) or {'state': 'PREPARING'})
    (module.DUMP / 'AAPL.json').write_text('{}\n')
    for payload in ({}, {'symbols': ['../bad']}, {'symbols': ['AAPL'], 'date': 'bad'},
                    {'symbols': ['AAPL'], 'date': '2026-07-14', 'speed': -1},
                    {'symbols': ['AAPL'], 'date': '2026-07-14', 'speed': 'nan'},
                    {'symbols': ['AAPL'], 'date': '2026-07-14', 'session': 'bad'},
                    {'symbols': ['MSFT'], 'date': '2026-07-14'}):
        assert client.post('/monitor/replay', json=payload).status_code == 400
    assert not calls
    response = client.post('/monitor/replay', json={'symbols': ['AAPL'], 'date': '2026-07-14', 'speed': 600, 'session': 'ah'})
    assert response.status_code == 202
    assert calls[0] == (['AAPL'], '2026-07-14', 600.0, 'ah', module.DUMP, False)


def test_replay_control_conflict_and_invalid_action_are_explicit(client, monkeypatch):
    from app.service.replay_service import replay_service, ReplayConflict
    def fail(action):
        raise ReplayConflict('ERR:OTHER_CLIENTS_CONNECTED')
    monkeypatch.setattr(replay_service, 'control', fail)
    response = client.post('/monitor/replay/control', json={'action': 'reset'})
    assert response.status_code == 409
    assert response.json['error'] == 'ERR:OTHER_CLIENTS_CONNECTED'


def test_deletion_refuses_archives_used_by_replay(client, monkeypatch):
    from app.service.replay_service import replay_service
    monkeypatch.setattr(replay_service, 'status', lambda: {'running': True, 'requested_symbols': ['AAPL']})
    (module.DUMP / 'AAPL.json').write_text('{}\n')
    snapshot()
    response = client.delete('/monitor/archives/AAPL', json={'confirm_symbol': 'AAPL', 'version': module.archive_version(module.DUMP / 'AAPL.json')})
    assert response.status_code == 409
    assert 'replay' in response.json['error']
