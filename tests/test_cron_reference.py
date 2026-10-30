import copy
import json
from datetime import date, timedelta

import numpy as np
import pytest

from app.service import cron_reference as cr
from watchtower_runtime import baseline_metrics_from_trades, WatchtowerRepository


@pytest.fixture
def baseline(tmp_path, monkeypatch):
    dates = [(date(2000, 1, 1) + timedelta(days=i)).isoformat() for i in range(1900)]
    values = np.random.default_rng(12).normal(.001, .01, len(dates))
    trades = [dict(open_datetime=d, close_datetime=d, pnl_pct=v*100, pnl=v*1000, value=1000)
              for d, v in zip(dates, values)]
    folder = tmp_path / 'run'
    folder.mkdir()
    (folder / 'trades.json').write_text(json.dumps(trades))
    # A deliberately different equity series ensures cron does not silently
    # substitute an equity source for its existing daily run samples.
    (folder / 'returns.csv').write_text('date,return\n'+'\n'.join(f'{d},0.5' for d in dates))
    monkeypatch.setattr(cr, 'baseline_run_directory', lambda *_: folder)
    row = dict(id=1, profile='live', window_start=dates[0], window_end=dates[-1])
    return row, folder, dates, trades


def overview():
    samples = [('2026-10-%02d' % i, .01 if i%2 else -.01) for i in range(1, 26)]
    stats = {'as_of_date': samples[-1][0]}
    for source in ('backtest', 'alpaca'):
        stats[source] = {str(n): {'recent': dict(daily_series=samples[-n:], metrics={'mean': .9}, sample_size=12)}
                         for n in (3, 5, 10, 15, 20, 25)}
    return dict(profile='live', reconciliation_statistics=stats)


def test_selected_baseline_reference_and_existing_samples_are_used_unchanged(baseline):
    selected, folder, dates, trades = baseline
    data = overview()
    original = copy.deepcopy(data)
    cr.attach_cron_reference(data, selected)
    for source in ('backtest', 'alpaca'):
        for n, window in data['reconciliation_statistics'][source].items():
            recent = window['recent']
            assert recent['daily_series'] == original['reconciliation_statistics'][source][n]['recent']['daily_series']
            assert recent['metrics'] == {'mean': .9}
            assert recent['historical_coherence']['historical_blocks'] == (1900-1020)//int(n)
            assert recent['historical_coherence']['reference_end'] == dates[-1]
            assert recent['balance_persistence']['threshold_mean'] == pytest.approx(
                np.mean([p for _, p in baseline_metrics_from_trades(trades, include_daily_series=True)['daily_series']]))
    selected['id'] = 2
    selected['window_end'] = dates[1799]
    cr.attach_cron_reference(data, selected)
    assert data['reconciliation_statistics']['baseline_id'] == 2
    assert data['reconciliation_statistics']['backtest']['3']['recent']['historical_coherence']['historical_blocks'] == (1800-1020)//3
    assert data['reconciliation_statistics']['backtest']['3']['recent']['daily_series'] == original['reconciliation_statistics']['backtest']['3']['recent']['daily_series']


def test_incomplete_sample_never_changes_reference_window_length(baseline):
    selected, *_ = baseline
    data = overview()
    data['reconciliation_statistics']['alpaca']['25']['recent']['daily_series'][0] = ('2026-10-01', None)
    cr.attach_cron_reference(data, selected)
    assert data['reconciliation_statistics']['alpaca']['25']['recent']['historical_coherence']['status'] == 'unavailable'
    assert data['reconciliation_statistics']['alpaca']['25']['recent']['effective_day_count'] == 24
    assert data['reconciliation_statistics']['alpaca']['3']['recent']['historical_coherence']['status'] != 'unavailable'
    assert data['reconciliation_statistics']['backtest']['25']['recent']['historical_coherence']['status'] != 'unavailable'
    cr.attach_cron_reference(data, None)
    assert 'baseline' in data['reconciliation_statistics']['backtest']['3']['recent']['historical_coherence']['reason']


def test_original_metrics_contract_is_unchanged_by_optional_series(baseline):
    trades = baseline[-1]
    original = baseline_metrics_from_trades(trades)
    extended = baseline_metrics_from_trades(trades, include_daily_series=True)
    assert 'daily_series' not in original
    extended.pop('daily_series')
    assert extended == original


def test_cron_samples_keep_missing_days_and_zero_and_do_not_change_alpaca(monkeypatch):
    import watchtower_runtime as wr
    monkeypatch.setattr(wr, '_load_cached_market_windows', lambda: [{'day':f'2026-10-0{i}'} for i in range(1,5)])
    settlements = [dict(trading_date='2026-10-01', source='alpaca', status='closed', qty=2, entry_price=100, pnl=10, pnl_pct=5)]
    saved = copy.deepcopy(settlements)
    rows = [dict(trading_date='2026-10-02', status='ok', summary={'bt_no_trade':True}, diffs=[])]
    result = WatchtowerRepository.reconciliation_statistics(rows, settlements=settlements, as_of=date(2026,10,4), windows=(3,))
    assert result['alpaca']['3']['recent']['daily_series'] == [('2026-10-01',.05)]
    assert result['backtest']['3']['recent']['daily_series'] == []
    assert {'date': '2026-10-02', 'reason': 'Giornata senza trade'} in result['backtest']['3']['recent']['excluded_days']
    assert settlements == saved


def test_overview_binds_selected_baseline_and_rejects_cross_profile(monkeypatch):
    from flask import Flask
    import app.watchtower as controller
    calls = []
    rows = [{'id':1}, {'id':2}]
    class Repo:
        def available(self): return True
        def profile_cockpit(self, profile): return {'profile':profile,'baselines':rows}
    monkeypatch.setattr(controller, 'repo', Repo())
    monkeypatch.setattr(controller, '_baselines_with_compatibility', lambda _: rows)
    monkeypatch.setattr(controller, 'attach_cron_reference', lambda overview, baseline: calls.append(baseline['id']))
    app = Flask(__name__)
    app.register_blueprint(controller.obs_bp, url_prefix='/dyn/obs')
    client = app.test_client()
    assert client.get('/dyn/obs/watchtower/cron/live/overview').status_code == 200
    assert client.get('/dyn/obs/watchtower/cron/live/overview?baseline_id=2').status_code == 200
    assert calls == [1,2]
    assert client.get('/dyn/obs/watchtower/cron/live/overview?baseline_id=999').status_code == 404
    assert client.get('/dyn/obs/watchtower/cron/live/overview?baseline_id=bad').status_code == 400
    assert calls == [1,2]


def test_open_day_excluded_even_with_some_closed_trades_and_zero_trade_is_valid(baseline, monkeypatch):
    import watchtower_runtime as wr
    selected, *_ = baseline
    monkeypatch.setattr(wr, '_load_cached_market_windows', lambda: [{'day':f'2026-10-0{i}'} for i in range(1,5)])
    settlements = [dict(trading_date=f'2026-10-0{i}', source='alpaca', status='closed', qty=2,
                        entry_price=100, pnl=0 if i == 1 else 10, pnl_pct=0 if i == 1 else 5) for i in range(1,4)]
    settlements.append(dict(trading_date='2026-10-03', source='alpaca', status='pending'))
    saved = copy.deepcopy(settlements)
    stats = WatchtowerRepository.reconciliation_statistics([], settlements=settlements, as_of=date(2026,10,4), windows=(3,))
    data = dict(profile='live', reconciliation_statistics=stats)
    cr.attach_cron_reference(data, selected)
    recent = stats['alpaca']['3']['recent']
    assert recent['daily_series'] == [('2026-10-01',0), ('2026-10-02',.05)]
    assert recent['sample_size'] == 2
    assert recent['metrics']['average_daily_return_pct'] == 2.5
    assert recent['effective_day_count'] == 2
    assert recent['historical_coherence']['status'] == 'unavailable'
    assert recent['excluded_days'][0]['date'] == '2026-10-03'
    assert settlements == saved
    assert stats['backtest']['3']['recent']['historical_coherence']['status'] == 'unavailable'


def test_reference_excludes_no_trade_and_open_days_but_retains_closed_zero(baseline):
    selected, folder, dates, trades = baseline
    trades.pop(0)
    trades[0]['pnl'] = trades[0]['pnl_pct'] = 0
    trades.append(dict(open_datetime=dates[2], pnl=5, value=1000, pnl_pct=.5))
    (folder / 'trades.json').write_text(json.dumps(trades))
    data = overview()
    cr.attach_cron_reference(data, selected)
    recent = data['reconciliation_statistics']['alpaca']['3']['recent']
    assert [row['date'] for row in recent['reference_excluded_days']] == [dates[0], dates[2]]
    reference, _ = cr._reference(str(folder), dates[0], dates[-1], (0,))
    assert (dates[1], 0) in reference


def test_windows_collect_n_valid_days_before_comparing_to_n_day_reference(baseline, monkeypatch):
    import watchtower_runtime as wr
    selected, *_ = baseline
    sessions = [(date(2026, 8, 1) + timedelta(days=i)).isoformat() for i in range(51)]
    monkeypatch.setattr(wr, '_load_cached_market_windows', lambda: [{'day':d} for d in sessions])
    settlements = []
    for source in ('alpaca', 'backtest'):
        for i, d in enumerate(sessions[:-1]):
            if i % 5 == 0:
                continue
            settlements.append(dict(trading_date=d, source=source,
                                    status='pending' if i % 7 == 0 else 'closed',
                                    qty=1, entry_price=100, pnl=i/10, pnl_pct=i/10))
    data = dict(profile='live', reconciliation_statistics=wr.WatchtowerRepository.reconciliation_statistics(
        [], settlements=settlements, as_of=date.fromisoformat(sessions[-1])))
    cr.attach_cron_reference(data, selected)
    expected = [d for i,d in enumerate(sessions[:-1]) if i % 5 and i % 7]
    for source in ('alpaca', 'backtest'):
        for n in (3,5,10,15,20,25):
            recent = data['reconciliation_statistics'][source][str(n)]['recent']
            assert [d for d,v in recent['daily_series']] == expected[-n:]
            assert recent['effective_day_count'] == n
            assert recent['historical_coherence']['historical_blocks'] == (1900-1020)//n
            assert recent['balance_persistence']['window_days'] == n
            assert recent['excluded_days']
