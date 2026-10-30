import json
import sqlite3
from pathlib import Path

from app.service.archive_chart_service import ArchiveChartService


def build(tmp_path, rows):
    path = tmp_path / 'AAPL.json'
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    service = ArchiveChartService(tmp_path / 'cache')
    key = service._key(path)
    service._build(path, key)
    assert service.inspect(path)['state'] == 'READY', service.inspect(path)
    return service, path


def quote(stamp, bid):
    return {'timestamp': stamp, 'event_type': 'quote', 'bid_price': bid, 'ask_price': bid + 1}


def test_archive_index_finds_windows_and_covers_entire_timespan(tmp_path):
    service, path = build(tmp_path, [
        quote('2026-07-14T13:30:00Z', 100), quote('2026-07-14T13:31:00Z', 101),
        quote('2026-07-14T20:00:00Z', 102), quote('2026-07-14T20:01:00Z', 103),
        quote('2026-07-15T13:30:00Z', 104),
    ])
    info = service.inspect(path)
    assert len(info['windows']) == 3
    assert [window['session'] for window in info['windows']] == ['RTH', 'After-hours', 'RTH']
    chart = service.chart(path)
    assert chart['window_count'] == 3
    assert chart['events'] == 5
    assert chart['start'] == chart['rows'][0]['time']
    assert chart['end'] == chart['rows'][-1]['time']
    chosen = service.chart(path, [2])
    assert [row['bid_price'] for row in chosen['rows']] == [102, 103]


def test_archive_sampling_is_bounded_and_retains_price_extrema(tmp_path):
    rows = []
    for hour in range(10, 20):
        for minute in range(60):
            rows.append(quote(f'2026-07-14T{hour:02}:{minute:02}:00Z', 500 if hour == 15 and minute == 20 else 100))
    service, path = build(tmp_path, rows)
    chart = service.chart(path, limit=30)
    assert chart['sampled'] is True
    assert len(chart['rows']) <= 42
    assert max(row['bid_price'] for row in chart['rows']) == 500
    assert chart['start'] == chart['rows'][0]['time']
    assert chart['end'] == chart['rows'][-1]['time']


def test_cached_windows_report_new_data_without_silently_reindexing(tmp_path):
    service, path = build(tmp_path, [quote('2026-07-14T20:00:00Z', 100)])
    with path.open('a') as handle:
        handle.write(json.dumps(quote('2026-07-15T20:00:00Z', 110)) + '\n')
    assert service.inspect(path)['stale'] is True
    assert service.inspect(path)['events'] == 1
    assert service.start(path)['state'] == 'READY'


def test_invalid_lines_do_not_prevent_valid_window_index(tmp_path):
    service, path = build(tmp_path, [quote('2026-07-14T20:00:00Z', 100), {'timestamp': 'bad'}])
    assert service.inspect(path)['invalid_lines'] == 1
    assert service.inspect(path)['events'] == 1


def test_native_bars_survive_dense_ticks_and_modes_never_mix(tmp_path):
    from datetime import datetime, timedelta, timezone
    origin = datetime(2026, 7, 14, 13, 30, tzinfo=timezone.utc)
    rows = [quote((origin + timedelta(seconds=i)).isoformat(), 100) for i in range(5000)]
    bars = [dict(timestamp=(origin + timedelta(minutes=i)).isoformat(), event_type='bar',
                 timeframe='intraday', open=100, high=102, low=99, close=101, volume=i+1) for i in range(80)]
    rows.extend(bars)
    rows.append(dict(bars[0], timeframe='daily', volume=999999))
    service, path = build(tmp_path, rows)
    native = service.chart(path, mode='bars', limit=5)
    assert len(native['rows']) == 80
    assert not native['sampled']
    assert native['rows'][-1]['volume'] == 80
    assert all(row['event_type'] == 'bar' and row['timeframe'] == 'intraday' for row in native['rows'])
    ticks = service.chart(path, mode='ticks', limit=30)
    assert ticks['sampled']
    assert all(row['event_type'] == 'quote' for row in ticks['rows'])
    minute = service.chart(path, mode='ticks', start=origin.timestamp()+60, end=origin.timestamp()+120)
    assert minute['visible_events'] == 61
    assert not minute['sampled']
    assert all(origin.timestamp()+60 <= row['time'] <= origin.timestamp()+120 for row in minute['rows'])


def test_old_index_requires_rebuild_without_changing_recording(tmp_path):
    service, path = build(tmp_path, [quote('2026-07-14T13:30:00Z', 100)])
    original = path.read_bytes()
    with sqlite3.connect(service._db_path(path)) as db:
        meta = json.loads(db.execute('SELECT payload FROM metadata').fetchone()[0])
        meta.pop('schema_version')
        db.execute('UPDATE metadata SET payload=?', (json.dumps(meta),))
    service.jobs.clear()
    assert service.inspect(path)['state'] == 'MISSING'
    service._build(path, service._key(path))
    assert service.inspect(path)['state'] == 'READY'
    assert path.read_bytes() == original
