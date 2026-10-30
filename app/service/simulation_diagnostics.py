"""Read-only diagnostics from a simulation's immutable artifacts.

Baseline trade metrics deliberately use Watchtower's implementation. Ordered
portfolio returns additionally support a descriptive historical comparison.
"""
import csv
import json
import math
from collections import defaultdict
from datetime import date

from watchtower_runtime import baseline_metrics_from_trades
from historical_coherence import (
    BALANCE_METHOD, compare_balance_windows, compare_windows,
    unavailable as coherence_unavailable,
)

WINDOWS = (3, 5, 10, 15, 20, 25)


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def day(value):
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return None


def read_json(path):
    if path.stat().st_size > 50 * 1024 * 1024:
        raise ValueError('Artefatto troppo grande per la diagnostica online (massimo 50 MB)')
    return json.loads(path.read_text(encoding='utf-8'))


def build_diagnostics(artifact, *, history_days=25):
    """`artifact` is the service's authorized, run-scoped artifact resolver."""
    warnings = []
    try:
        data = read_json(artifact('trades.json'))
        trades = data if isinstance(data, list) else list(data.values()) if isinstance(data, dict) else []
        trades = [t for t in trades if isinstance(t, dict)]
    except FileNotFoundError:
        trades = []
        warnings.append('trades.json non disponibile: statistiche e movimenti dei trade non ricostruibili.')
    except ValueError as exc:
        trades = []
        warnings.append(f'trades.json non leggibile: {exc}. Statistiche e movimenti dei trade non ricostruibili; il confronto del portafoglio usa returns.csv.')

    # PyFolio returns are portfolio returns. Compound intraday rows per date,
    # retaining zero-return days. Never use the downsampled equity endpoint.
    daily_returns = {}
    invalid_returns = False
    try:
        with artifact('returns.csv').open(newline='', encoding='utf-8') as stream:
            reader = csv.reader(stream)
            next(reader, None)
            for row in reader:
                if len(row) < 2:
                    if row:
                        invalid_returns = True
                    continue
                session, value = day(row[0]), number(row[-1])
                if session and value is not None:
                    if value <= -1:
                        invalid_returns = True
                    daily_returns[session] = (1 + daily_returns.get(session, 0)) * (1 + value) - 1
                else:
                    invalid_returns = True
    except FileNotFoundError:
        warnings.append('returns.csv non disponibile: finestre basate solo sulle date dei trade; giorni senza trade non ricostruibili.')

    # PyFolio positions.csv records end-of-date market values (not quantities).
    # It also covers holdings that have not yet produced a closed trade.
    holdings = {}
    try:
        with artifact('positions.csv').open(newline='', encoding='utf-8') as stream:
            reader = csv.DictReader(stream)
            date_key = reader.fieldnames[0] if reader.fieldnames else None
            for row in reader:
                session = day(row.get(date_key))
                if session:
                    holdings[session] = [dict(asset=symbol, market_value=number(value))
                                         for symbol, value in row.items()
                                         if symbol != date_key and symbol.lower() != 'cash'
                                         and number(value) not in (None, 0)]
    except FileNotFoundError:
        warnings.append('positions.csv non disponibile: posizioni a fine giornata non verificabili.')

    opened, closed, by_entry = defaultdict(list), defaultdict(list), defaultdict(list)
    closed_trades = []
    undated = 0
    for index, trade in enumerate(trades):
        entry = day(trade.get('entry_signal_dt') or trade.get('open_datetime') or trade.get('entry_datetime'))
        actual_entry = day(trade.get('open_datetime') or trade.get('entry_datetime'))
        exit_day = day(trade.get('close_datetime') or trade.get('exit_datetime'))
        item = dict(trade, key=str(index))
        if actual_entry:
            opened[actual_entry].append(item)
        if exit_day:
            closed[exit_day].append(item)
            if number(trade.get('pnl_pct', trade.get('pnl_percent'))) is not None:
                closed_trades.append(trade)
                if entry:
                    by_entry[entry].append(trade)
                else:
                    undated += 1
    if undated:
        warnings.append(f'{undated} trade chiusi senza data di ingresso: inclusi nella baseline, esclusi dalle finestre.')

    # Missing returns do not fabricate zero-return sessions. Include trade event
    # dates for the daily detail, but do not let an exit outside the return
    # calendar shift the statistical window.
    sessions = sorted(daily_returns or set(by_entry))
    baseline_metrics = baseline_metrics_from_trades(closed_trades)
    baseline = dict(metrics=baseline_metrics, sample_size=baseline_metrics['sample_size'],
                    window_start=sessions[0] if sessions else None,
                    window_end=sessions[-1] if sessions else None)
    if invalid_returns:
        reason = 'returns.csv contiene righe mancanti o non valide; il confronto della serie non è calcolabile.'
        coherence = {n: coherence_unavailable(reason) for n in WINDOWS}
        balance = {n: coherence_unavailable(reason, method=BALANCE_METHOD) for n in WINDOWS}
    else:
        ordered_returns = sorted(daily_returns.items())
        coherence = compare_windows(ordered_returns, WINDOWS)
        balance = compare_balance_windows(ordered_returns, WINDOWS)
    windows = []
    for count in WINDOWS:
        dates = sessions[-count:]
        recent_trades = [trade for session in dates for trade in by_entry[session]]
        metrics = baseline_metrics_from_trades(recent_trades)
        recent = dict(metrics=metrics, sample_size=metrics['sample_size'],
                      window_start=dates[0] if dates else None,
                      window_end=dates[-1] if dates else None,
                      historical_coherence=coherence[count], balance_persistence=balance[count])
        # A short run must not masquerade as a complete N-session window.
        windows.append(dict(days=count, comparison=dict(recent=recent) if len(dates) == count else None))

    all_days = sorted(set(sessions) | set(opened) | set(closed) | set(holdings), reverse=True)
    rows = []
    for session in all_days[:history_days]:
        entries, exits = opened[session], closed[session]
        rows.append(dict(key=session, trading_date=session, statusLabel='Backtest', badgeColor='primary',
                         day_return=daily_returns[session] * 100 if session in daily_returns else None,
                         detail=f'{len(entries)} aperture · {len(exits)} chiusure',
                         opened=entries, closed=exits, holdings=holdings.get(session)))
    positions = []
    try:
        snapshot = read_json(artifact('positions.json'))
        if isinstance(snapshot, dict):
            positions = [dict(value, asset=symbol) for symbol, value in snapshot.items()
                         if isinstance(value, dict) and number(value.get('size')) not in (None, 0)]
    except FileNotFoundError:
        warnings.append('positions.json non disponibile: posizioni aperte a fine run non verificabili.')
    return finite_json(dict(baseline=baseline, windows=windows, rows=rows, total_days=len(all_days),
                open_positions=positions, warnings=warnings,
                calendar_source='returns.csv' if daily_returns else 'trades.json'))


def finite_json(value):
    """Distribution metrics may contain infinite profit factors; emit strict JSON."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: finite_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [finite_json(item) for item in value]
    return value
