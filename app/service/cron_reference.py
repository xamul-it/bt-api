"""Connect the selected cron baseline to the samples already computed by cron.

Only input preparation is cron-specific. All three statistical calculations and
their presentation contract are shared with simulation diagnostics.
"""
import csv
import re
from functools import lru_cache
from pathlib import Path

import profile_baseline as pbl
from historical_coherence import BALANCE_METHOD, compare_reference_windows, unavailable
from watchtower_runtime import baseline_metrics_from_trades, _trade_close_dt
from app.service.simulation_diagnostics import day, read_json


def baseline_run_directory(profile, baseline, *, required_artifacts=('trades.json', 'returns.csv')):
    strategy = str(baseline.get('strategy') or '').split('.')
    run_id = str(baseline.get('run_id') or '')
    if len(strategy) != 2 or not all(re.fullmatch(r'[A-Za-z0-9_]+', part) for part in strategy):
        raise ValueError('Identità della strategia della baseline non valida.')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', run_id):
        raise ValueError('Identità del run della baseline non valida.')
    roots = [pbl.BT_CORE / 'out']
    config = pbl.read_env_file(pbl.SCHEDULED_DIR / f'{profile}.env')
    if config.get('CODE_ROOT'):
        roots.append(Path(config['CODE_ROOT']) / 'bt-core' / 'out')
    for root in roots:
        root = root.resolve()
        directory = (root / strategy[0].lower() / strategy[1] / run_id).resolve()
        if directory.is_relative_to(root) and all(
            (directory / name).resolve().is_relative_to(root) and (directory / name).is_file()
            for name in required_artifacts
        ):
            return directory
    raise ValueError('Artefatti del run della baseline selezionata non disponibili.')


@lru_cache(maxsize=16)
def _reference(directory, start, end, artifact_versions):
    directory = Path(directory)
    trades = read_json(directory / 'trades.json')
    if not isinstance(trades, list):
        raise ValueError('Trade della baseline non leggibili.')
    # Cron samples are the existing daily entry-session gains. Use exactly the
    # same producer for historical gains, never compare them to portfolio-equity
    # returns or to unweighted per-symbol returns. The run supplies its calendar.
    open_days = {day(trade.get('entry_signal_dt') or trade.get('open_datetime') or trade.get('entry_datetime'))
                 for trade in trades if _trade_close_dt(trade) is None}
    metrics = baseline_metrics_from_trades(trades, include_daily_series=True)
    gains = dict(metrics['daily_series'])
    sessions = set()
    with (directory / 'returns.csv').open(newline='', encoding='utf-8') as stream:
        reader = csv.reader(stream)
        next(reader, None)
        for row in reader:
            if not row:
                continue
            session = day(row[0])
            if not session:
                raise ValueError('Calendario del run della baseline non valido.')
            if start <= session <= end:
                sessions.add(session)
    # Only sessions with realised trade results belong to the cron reference.
    excluded = [dict(date=session, reason=('Giornata aperta' if session in open_days else 'Giornata senza trade conclusi'))
                for session in sorted(sessions) if session not in gains or session in open_days]
    return ([(session, gains[session]) for session in sorted(sessions) if session in gains and session not in open_days], excluded)


def attach_cron_reference(overview, baseline):
    statistics = overview.get('reconciliation_statistics') or {}
    if not statistics:
        return
    reason = None
    reference = []
    reference_excluded = []
    try:
        if not baseline:
            raise ValueError('Seleziona una baseline per il riferimento storico.')
        start, end = day(baseline.get('window_start')), day(baseline.get('window_end'))
        if not start or not end or start > end:
            raise ValueError('Date della baseline non valide.')
        directory = baseline_run_directory(overview['profile'], baseline)
        versions = tuple((directory / name).stat().st_mtime_ns for name in ('trades.json', 'returns.csv'))
        reference, reference_excluded = _reference(str(directory), start, end, versions)
        if not reference:
            raise ValueError('Nessuna seduta disponibile nelle date della baseline.')
    except (OSError, ValueError) as exc:
        reason = str(exc)
    statistics['baseline_id'] = baseline.get('id') if baseline else None
    for source in ('backtest', 'alpaca'):
        windows = statistics.get(source) or {}
        samples = {int(n): w['recent'].get('daily_series', []) for n, w in windows.items() if w.get('recent')}
        for n, sample in samples.items():
            recent = windows[str(n)]['recent']
            excluded = {row['date']: row['reason'] for row in recent.get('excluded_days', [])}
            for session, value in sample:
                if value is None:
                    excluded.setdefault(session, 'Risultato giornaliero non disponibile')
            valid = [(session, value) for session, value in sample if session not in excluded]
            recent['excluded_days'] = [dict(date=session, reason=why) for session, why in sorted(excluded.items())]
            recent['effective_day_count'] = len(valid)
            recent['reference_excluded_days'] = reference_excluded
            sample_reason = reason or (f'Servono {n} giornate con trade conclusi; disponibili {len(valid)}.' if len(valid) != n else None)
            indicators = (dict(historical_coherence=unavailable(sample_reason),
                               balance_persistence=unavailable(sample_reason, method=BALANCE_METHOD))
                          if sample_reason else compare_reference_windows(reference, {n: valid})[n])
            for indicator in indicators.values():
                indicator.update(unit='daily_entry_return', reference_source='profile_baseline',
                                 baseline_id=statistics['baseline_id'])
            recent.update(indicators)
