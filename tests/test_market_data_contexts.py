from flask import Flask
from app import live, quote_monitor


def test_market_data_contexts_only_return_public_account_fields(monkeypatch):
    app = Flask(__name__)
    app.register_blueprint(live.al_bp, url_prefix='/al')
    monkeypatch.setattr(live.repo, 'available', lambda: True)
    monkeypatch.setattr(live.repo, 'list_alpaca_portfolios', lambda active_only: [dict(
        portfolio_key_id='p', display_name='Account', paper=True, alpaca_api_key='private')])
    monkeypatch.setattr(live.repo, 'list_assigned_portfolios', lambda: [dict(portfolio_key_id='p', assigned_profile='research')])
    response = app.test_client().get('/al/contexts')
    assert response.status_code == 200
    assert response.json == [dict(portfolio_key_id='p', display_name='Account', paper=True, profile='research')]
    assert b'private' not in response.data


def test_history_never_falls_back_to_environment_for_unknown_account(monkeypatch):
    app = Flask(__name__)
    app.register_blueprint(quote_monitor.quote_monitor_bp, url_prefix='/monitor')
    monkeypatch.setattr(live.repo, 'available', lambda: True)
    def unknown(_):
        raise ValueError('Unknown account')
    monkeypatch.setattr(live.repo, 'resolve_alpaca_portfolio', unknown)
    response = app.test_client().get('/monitor/history/AAPL?portfolio_key_id=unknown&date=2026-07-14')
    assert response.status_code == 404
