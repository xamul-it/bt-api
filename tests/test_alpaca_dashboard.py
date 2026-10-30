from flask import Flask
import pytest
from app import live


@pytest.fixture
def client(monkeypatch):
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.register_blueprint(live.al_bp, url_prefix='/al')
    monkeypatch.setattr(live, 'ENABLE_ORIGIN_CHECK', False)
    return app.test_client()


def test_contexts_never_expose_credentials(client, monkeypatch):
    monkeypatch.setattr(live.repo, 'available', lambda: True)
    monkeypatch.setattr(live.repo, 'list_alpaca_portfolios', lambda active_only: [dict(
        portfolio_key_id='p1', display_name='Mirror', paper=True,
        alpaca_api_key='secret-key', alpaca_secret_key='secret-secret', metadata={'secret': 'private'})])
    monkeypatch.setattr(live.repo, 'list_assigned_portfolios', lambda: [dict(portfolio_key_id='p1', assigned_profile='mirror')])
    response = client.get('/al/contexts')
    assert response.status_code == 200
    assert response.json == [dict(portfolio_key_id='p1', display_name='Mirror', paper=True, profile='mirror')]
    assert b'secret' not in response.data


@pytest.mark.parametrize('available,exception,status', [(False, None, 503), (True, ValueError('unknown'), 404), (True, RuntimeError('offline'), 503)])
def test_explicit_context_never_falls_back_to_environment(client, monkeypatch, available, exception, status):
    monkeypatch.setattr(live.repo, 'available', lambda: available)
    def resolve(_):
        raise exception
    monkeypatch.setattr(live.repo, 'resolve_alpaca_portfolio', resolve)
    monkeypatch.setattr(live.requests, 'get', lambda *a, **k: pytest.fail('Must not contact fallback account'))
    response = client.get('/al/orders?portfolio_key_id=unknown')
    assert response.status_code == status


@pytest.mark.parametrize('paper,host', [(True, 'https://paper-api.alpaca.markets'), (False, 'https://api.alpaca.markets')])
def test_selected_account_controls_host_and_credentials(client, monkeypatch, paper, host):
    monkeypatch.setattr(live, '_resolve_portfolio_credentials', lambda key: ('chosen', 'secret', paper, key, 'Account'))
    seen = []
    class Response:
        status_code = 200
        def json(self):
            return [{'id': 'order1'}]
    def get(url, **kwargs):
        seen.append((url, kwargs))
        return Response()
    monkeypatch.setattr(live.requests, 'get', get)
    response = client.get('/al/orders?portfolio_key_id=p1&after=2026-10-20T00:00:00Z')
    assert response.status_code == 200
    assert response.json['orders'] == [{'id': 'order1'}]
    assert seen[0][0] == f'{host}/v2/orders'
    assert seen[0][1]['headers']['APCA-API-KEY-ID'] == 'chosen'
    assert seen[0][1]['params']['after'] == '2026-10-20T00:00:00Z'


def test_orders_reject_invalid_filters_before_contacting_broker(client, monkeypatch):
    monkeypatch.setattr(live, '_alpaca_get', lambda *a, **k: pytest.fail('Invalid request reached broker'))
    for query in ('', '?portfolio_key_id=p&status=bad', '?portfolio_key_id=p&after=bad'):
        assert client.get('/al/orders' + query).status_code == 400


def test_order_limit_is_explicit(client, monkeypatch):
    monkeypatch.setattr(live, '_alpaca_get', lambda *a, **k: ([{'id': str(i)} for i in range(500)], None))
    response = client.get('/al/orders?portfolio_key_id=p')
    assert response.json['truncated'] is True


@pytest.mark.parametrize('candidate,allowed,expected', [
    ('https://ilz.duckdns.org:1443/dash/', 'https://ilz.duckdns.org:1443', True),
    ('https://ilz.duckdns.org/dash/#/Alpaca', 'https://ilz.duckdns.org', True),
    ('https://ilz.duckdns.org:443/dash/', 'https://ilz.duckdns.org', True),
    ('https://ilz.duckdns.org:1443/dash/', 'https://ilz.duckdns.org', False),
    ('https://ilz.duckdns.org:1443/dash/', 'https://ilz.duckdns.org:*', True),
    ('http://ilz.duckdns.org:1443/dash/', 'https://ilz.duckdns.org:*', False),
    ('https://ilz.duckdns.org.evil.test/dash/', 'https://ilz.duckdns.org:*', False),
    ('https://ilz.duckdns.org@evil.test/dash/', 'https://ilz.duckdns.org:*', False),
    ('https://ilz.duckdns.org:bad/dash/', 'https://ilz.duckdns.org:*', False),
    ('null', 'https://ilz.duckdns.org', False),
])
def test_origin_comparison(candidate, allowed, expected):
    assert live._origin_matches(candidate, allowed) is expected


def test_browser_referer_without_origin_is_accepted(client, monkeypatch):
    monkeypatch.setattr(live, 'ENABLE_ORIGIN_CHECK', True)
    monkeypatch.setattr(live, 'ALLOWED_ORIGINS', 'https://ilz.duckdns.org,https://ilz.duckdns.org:1443')
    monkeypatch.setattr(live.repo, 'available', lambda: True)
    monkeypatch.setattr(live.repo, 'list_alpaca_portfolios', lambda active_only: [])
    monkeypatch.setattr(live.repo, 'list_assigned_portfolios', lambda: [])
    response = client.get('/al/contexts', headers={'Referer': 'https://ilz.duckdns.org:1443/dash/'})
    assert response.status_code == 200
    response = client.get('/al/contexts', headers={
        'Origin': 'https://evil.test', 'Referer': 'https://ilz.duckdns.org:1443/dash/'})
    assert response.status_code == 403
