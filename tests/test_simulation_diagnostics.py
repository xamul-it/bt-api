import json
from datetime import date, timedelta

import pytest

from app.service.simulation_diagnostics import build_diagnostics


@pytest.fixture
def artifacts(tmp_path):
    def write(name, value):
        path = tmp_path / name
        path.write_text(json.dumps(value) if name.endswith('.json') else value)
        return path

    def resolve(name):
        path = tmp_path / name
        if not path.is_file():
            raise FileNotFoundError(name)
        return path

    return write, resolve


def test_windows_use_run_dates_zero_trade_days_and_full_history(artifacts):
    write, resolve = artifacts
    dates = [(date(2020, 1, 1) + timedelta(days=i)).isoformat() for i in range(30)]
    write('returns.csv', ',return\n' + '\n'.join(f'{d},0' for d in dates))
    trades = [dict(asset='AAA', entry_signal_dt=dates[i], open_datetime=dates[i],
                   close_datetime=dates[i + 1], pnl_pct=i - 10, pnl=i - 10,
                   value=100, size=1, price=100) for i in (0, 20, 27, 28)]
    write('trades.json', trades)
    write('positions.json', {'OPEN': dict(size=2, price=50, current=51), 'ZERO': dict(size=0)})
    write('positions.csv', ',AAA,OPEN,cash\n' + f'{dates[-1]},0,102,1000\n')

    result = build_diagnostics(resolve, history_days=2)

    assert result['baseline']['sample_size'] == 4
    assert result['baseline']['metrics']['mean'] == pytest.approx(8.75)
    assert result['baseline']['window_start'] == dates[0]
    recent = result['windows'][0]['comparison']['recent']
    assert recent['window_start'] == dates[-3]
    assert recent['window_end'] == dates[-1]
    assert recent['sample_size'] == 2
    assert recent['metrics']['mean'] == pytest.approx(17.5)
    assert recent['metrics']['average_daily_return_pct'] == pytest.approx(17.5)
    assert result['total_days'] == 30
    assert len(result['rows']) == 2
    last = result['rows'][0]
    assert last['day_return'] == 0
    assert len(last['closed']) == 1
    assert not last['opened']
    assert last['holdings'] == [dict(asset='OPEN', market_value=102)]
    assert result['open_positions'][0]['asset'] == 'OPEN'
    assert len(result['open_positions']) == 1
    assert not result['warnings']


def test_short_history_missing_artifacts_and_intraday_compounding(artifacts):
    write, resolve = artifacts
    write('returns.csv', ',return\n2020-01-01 10:00:00,0.1\n2020-01-01 11:00:00,-0.1\n2020-01-02,0\n')
    result = build_diagnostics(resolve)
    assert result['rows'][1]['day_return'] == pytest.approx(-1)
    assert result['rows'][0]['day_return'] == 0
    assert all(window['comparison'] is None for window in result['windows'])
    assert result['baseline']['sample_size'] == 0
    assert len(result['warnings']) == 3
    assert result['rows'][0]['holdings'] is None


def test_strict_json_and_closed_trade_only_statistics(artifacts):
    write, resolve = artifacts
    write('trades.json', [
        dict(asset='CLOSED', open_datetime='2020-01-01', close_datetime='2020-01-02', pnl_pct=2),
        dict(asset='OPEN', open_datetime='2020-01-02', close_datetime=None, pnl_pct=100),
    ])
    result = build_diagnostics(resolve)
    assert result['baseline']['sample_size'] == 1
    assert result['baseline']['metrics']['mean'] == 2
    assert result['calendar_source'] == 'trades.json'
    json.dumps(result, allow_nan=False)
    assert any('senza trade' in warning for warning in result['warnings'])


def test_coherence_uses_ordered_portfolio_returns_and_only_previous_history(artifacts):
    import numpy as np
    write, resolve = artifacts
    rng = np.random.default_rng(20261024)
    values = rng.normal(0, .01, 1800)
    values[-25:] = -.10
    dates = [(date(2000, 1, 1) + timedelta(days=i)).isoformat() for i in range(len(values))]
    write('returns.csv', ',return\n' + '\n'.join(f'{d},{v}' for d, v in zip(dates, values)))
    write('trades.json', [])
    result = build_diagnostics(resolve, history_days=1)
    for window in result['windows']:
        indicator = window['comparison']['recent']['historical_coherence']
        assert indicator['status'] == 'very_few_precedents'
        assert indicator['unit'] == 'daily_portfolio_return'
        assert indicator['reference_end'] == dates[-26]
        assert indicator['tail_blocks'] == 0
        balance = window['comparison']['recent']['balance_persistence']
        assert balance['status'] == 'available'
        assert balance['threshold_mean'] == pytest.approx(values[:-25].mean())
        assert balance['reference_end'] == dates[-26]
        assert balance['recent']['negative_days'] == window['days']
        assert balance['recent']['longest_negative_run'] == window['days']
        assert balance['ongoing_negative_days'] >= 25
        for family in ('balance', 'persistence'):
            rank = balance[family]
            assert 0 <= rank['tail_blocks'] <= rank['historical_blocks']
            if window['days'] >= 15:
                assert rank['tail_blocks'] == 0
    # The number of rows selected for display cannot change the indicator.
    assert build_diagnostics(resolve, history_days=100)['windows'] == result['windows']


def test_coherence_never_fills_invalid_returns_or_uses_trades_as_portfolio_returns(artifacts):
    write, resolve = artifacts
    write('returns.csv', ',return\n2020-01-01,0.01\n2020-01-02,not-a-number\n2020-01-03,0\n2020-01-04,0\n')
    result = build_diagnostics(resolve)
    indicator = result['windows'][0]['comparison']['recent']['historical_coherence']
    assert indicator['status'] == 'unavailable'
    assert 'non valide' in indicator['reason']
    assert result['windows'][0]['comparison']['recent']['balance_persistence']['status'] == 'unavailable'
    write('returns.csv', ',return\n')
    write('trades.json', [dict(open_datetime=f'2020-01-0{i}', close_datetime=f'2020-01-0{i+1}', pnl_pct=2) for i in range(1, 5)])
    result = build_diagnostics(resolve)
    assert result['windows'][0]['comparison']['recent']['historical_coherence']['status'] == 'unavailable'
    assert result['windows'][0]['comparison']['recent']['balance_persistence']['status'] == 'unavailable'


def test_large_trade_artifact_does_not_hide_portfolio_coherence(artifacts):
    import math
    write, resolve = artifacts
    dates = [(date(2000, 1, 1) + timedelta(days=i)).isoformat() for i in range(1800)]
    write('returns.csv', ',return\n' + '\n'.join(f'{d},{.01*math.sin(i)}' for i, d in enumerate(dates)))
    path = write('trades.json', [])
    with path.open('wb') as stream:
        stream.seek(50 * 1024 * 1024)
        stream.write(b']')
    result = build_diagnostics(resolve)
    assert result['windows'][0]['comparison']['recent']['historical_coherence']['status'] != 'unavailable'
    assert result['windows'][0]['comparison']['recent']['balance_persistence']['status'] == 'available'
    assert any('troppo grande' in warning for warning in result['warnings'])


def test_endpoint_scopes_artifacts_to_selected_job_and_rejects_missing_job(monkeypatch, artifacts):
    from flask import Flask
    from app import simulations as views
    write, resolve = artifacts
    write('returns.csv', ',return\n2020-01-01,0\n')
    calls = []

    class Service:
        def job(self, job_id):
            calls.append(job_id)
            if job_id != 'selected':
                raise LookupError('Simulazione non trovata')
            return {'id': job_id}

        def artifact(self, job_id, filename):
            assert job_id == 'selected'
            return resolve(filename)

    monkeypatch.setattr(views, 'service', Service())
    app = Flask(__name__)
    app.register_blueprint(views.sim_bp, url_prefix='/sim')
    client = app.test_client()
    response = client.get('/sim/jobs/selected/diagnostics?history_days=1')
    assert response.status_code == 200
    assert len(response.json['rows']) == 1
    assert client.get('/sim/jobs/missing/diagnostics').status_code == 404
    assert client.get('/sim/jobs/selected/diagnostics?history_days=wrong').status_code == 400


def test_diagnostics_requires_authenticated_session(monkeypatch, artifacts):
    from flask import Flask
    from app import simulations as views
    from app.auth import init_auth
    write, resolve = artifacts
    write('returns.csv', ',return\n2020-01-01,0\n')

    class Service:
        def job(self, job_id):
            return {'id': job_id}

        def artifact(self, job_id, filename):
            return resolve(filename)

    monkeypatch.setattr(views, 'service', Service())
    app = Flask(__name__)
    init_auth(app)
    app.config['SESSION_COOKIE_SECURE'] = False
    app.register_blueprint(views.sim_bp, url_prefix='/dyn/simulations')
    client = app.test_client()
    assert client.get('/dyn/simulations/jobs/selected/diagnostics').status_code == 401
    with client.session_transaction() as session:
        session['authed'] = True
    response = client.get('/dyn/simulations/jobs/selected/diagnostics')
    assert response.status_code == 200
    assert response.json['rows'][0]['trading_date'] == '2020-01-01'
