"""Versioned archive index and bounded chart samples covering the entire recording."""
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.paths import CONFIG_PATH

LEVELS = (60, 900, 3600, 86400, 604800)
NY = ZoneInfo('America/New_York')


def timestamp_ns(value):
    dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1e9)


def session_label(ns):
    dt = datetime.fromtimestamp(ns / 1e9, NY)
    minute = dt.hour * 60 + dt.minute
    session = 'Pre-market' if 240 <= minute < 570 else 'RTH' if 570 <= minute < 960 else 'After-hours' if 960 <= minute < 1200 else 'Fuori sessione'
    return dt.date().isoformat(), session


class ArchiveChartService:
    def __init__(self, cache_dir=None):
        self.cache_dir = Path(cache_dir or Path(CONFIG_PATH) / 'archive-index')
        self.jobs = {}
        self.lock = threading.RLock()
        self.worker = threading.Semaphore(1)

    def _key(self, path):
        return hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:24]

    def _db_path(self, path):
        return self.cache_dir / f'{self._key(path)}.sqlite'

    def inspect(self, path):
        key = self._key(path)
        with self.lock:
            job = dict(self.jobs.get(key, {}))
        cache = self._db_path(path)
        if not cache.exists():
            return {'state': job.get('state', 'MISSING'), 'progress': job.get('progress', 0), 'error': job.get('error'), 'windows': []}
        with sqlite3.connect(cache) as db:
            db.row_factory = sqlite3.Row
            meta = json.loads(db.execute('SELECT payload FROM metadata').fetchone()[0])
            if meta.get('schema_version') != 2:
                return {'state': job.get('state', 'MISSING'), 'progress': job.get('progress', 0), 'error': job.get('error'), 'windows': []}
            windows = [dict(row) for row in db.execute('SELECT * FROM windows ORDER BY start_ns')]
        stat = path.stat()
        return {**meta, 'state': job.get('state', 'READY'), 'progress': job.get('progress', 1),
                'error': job.get('error'), 'windows': windows,
                'stale': stat.st_size != meta['source_bytes'] or stat.st_mtime_ns != meta['source_mtime_ns']}

    def start(self, path, force=False):
        key = self._key(path)
        with self.lock:
            if self.jobs.get(key, {}).get('state') in ('QUEUED', 'INDEXING'):
                return dict(self.jobs[key])
            if self._db_path(path).exists() and not force and self.inspect(path).get('state') != 'MISSING':
                return {'state': 'READY'}
            self.jobs[key] = {'state': 'QUEUED', 'progress': 0}
        threading.Thread(target=self._build, args=(path, key), daemon=True, name='archive-index').start()
        return {'state': 'QUEUED', 'progress': 0}

    def _build(self, path, key):
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_dir / f'{key}.{uuid.uuid4().hex}.building'
        try:
            with self.worker:
                with self.lock:
                    self.jobs[key] = {'state': 'INDEXING', 'progress': 0}
                stat = path.stat()
                db = sqlite3.connect(temporary)
                try:
                    db.execute('PRAGMA journal_mode=OFF')
                    db.execute('PRAGMA cache_size=-8192')
                    db.execute('CREATE TABLE events(id INTEGER PRIMARY KEY,ns INTEGER,kind TEXT,bid REAL,ask REAL,price REAL,close REAL,open REAL,high REAL,low REAL,volume REAL,timeframe TEXT)')
                    db.execute('CREATE TABLE windows(id INTEGER PRIMARY KEY,start_ns INTEGER,end_ns INTEGER,event_count INTEGER,quotes INTEGER,trades INTEGER,bars INTEGER,date TEXT,session TEXT)')
                    db.execute('CREATE TABLE overview(level INTEGER,bucket INTEGER,window_id INTEGER,kind TEXT,first_id INTEGER,last_id INTEGER,min_id INTEGER,max_id INTEGER,ask_min_id INTEGER,ask_max_id INTEGER)')
                    db.execute('CREATE TABLE metadata(payload TEXT)')
                    pending = []
                    invalid = 0
                    with path.open('rb') as handle:
                        while handle.tell() < stat.st_size:
                            line = handle.readline(1_000_001)
                            if not line or handle.tell() > stat.st_size:
                                break
                            if len(line) > 1_000_000:
                                raise ValueError('Riga archivio troppo lunga')
                            try:
                                entry = json.loads(line)
                                ns = timestamp_ns(entry.get('timestamp'))
                                kind = entry.get('event_type') or entry.get('timeframe') or 'bar'
                                kind = kind if kind in ('quote', 'trade') else 'bar'
                                values = [entry.get(field) for field in ('bid_price', 'ask_price', 'price', 'close', 'open', 'high', 'low', 'volume')]
                                values = [float(value) if value is not None and math.isfinite(float(value)) else None for value in values]
                                pending.append((ns, kind, *values, entry.get('timeframe') or 'intraday'))
                            except (ValueError, TypeError, AttributeError, OverflowError):
                                invalid += 1
                            if len(pending) >= 5000:
                                db.executemany('INSERT INTO events(ns,kind,bid,ask,price,close,open,high,low,volume,timeframe) VALUES(?,?,?,?,?,?,?,?,?,?,?)', pending)
                                pending.clear()
                                with self.lock:
                                    self.jobs[key]['progress'] = handle.tell() / max(1, stat.st_size) * 0.8
                    if pending:
                        db.executemany('INSERT INTO events(ns,kind,bid,ask,price,close,open,high,low,volume,timeframe) VALUES(?,?,?,?,?,?,?,?,?,?,?)', pending)
                    db.execute('CREATE INDEX event_clock ON events(ns,id)')
                    active = {}
                    window = None
                    previous = None
                    window_id = 0
                    count = 0
                    def flush(item):
                        db.execute('INSERT INTO overview VALUES(?,?,?,?,?,?,?,?,?,?)', item['row'])
                    def flush_window():
                        if window:
                            db.execute('INSERT INTO windows VALUES(?,?,?,?,?,?,?,?,?)', window)
                    for row in db.execute('SELECT id,ns,kind,bid,ask,price,close FROM events ORDER BY ns,id'):
                        event_id, ns, kind, bid, ask, price, close = row
                        day, session = session_label(ns)
                        if window is None or day != window[7] or session != window[8] or ns - previous > 1800 * 1e9:
                            flush_window()
                            window_id += 1
                            window = [window_id, ns, ns, 0, 0, 0, 0, day, session]
                        window[2] = ns
                        window[3] += 1
                        window[{'quote': 4, 'trade': 5, 'bar': 6}[kind]] += 1
                        previous = ns
                        count += 1
                        metric = bid if kind == 'quote' else price if kind == 'trade' else close
                        metric = metric if metric is not None else 0
                        ask_value = ask if ask is not None else metric
                        for level in LEVELS:
                            bucket = ns // (level * 1_000_000_000)
                            slot = (level, kind)
                            item = active.get(slot)
                            if item is None or item['row'][1:3] != [bucket, window_id]:
                                if item:
                                    flush(item)
                                item = {'row': [level, bucket, window_id, kind, event_id, event_id, event_id, event_id, event_id, event_id],
                                        'min': metric, 'max': metric, 'ask_min': ask_value, 'ask_max': ask_value}
                                active[slot] = item
                            item['row'][5] = event_id
                            for field, value, column, compare in [('min', metric, 6, min), ('max', metric, 7, max), ('ask_min', ask_value, 8, min), ('ask_max', ask_value, 9, max)]:
                                if compare(item[field], value) != item[field]:
                                    item[field] = value
                                    item['row'][column] = event_id
                        if count % 20000 == 0:
                            with self.lock:
                                self.jobs[key]['progress'] = 0.85
                    flush_window()
                    for item in active.values():
                        flush(item)
                    db.execute('CREATE INDEX overview_lookup ON overview(level,window_id,bucket)')
                    meta = {'schema_version': 2, 'source_bytes': stat.st_size, 'source_mtime_ns': stat.st_mtime_ns,
                            'events': count, 'invalid_lines': invalid, 'indexed_at': time.time()}
                    db.execute('INSERT INTO metadata VALUES(?)', (json.dumps(meta),))
                    db.commit()
                finally:
                    db.close()
                temporary.replace(self._db_path(path))
                with self.lock:
                    self.jobs[key] = {'state': 'READY', 'progress': 1}
        except Exception as exc:
            with self.lock:
                self.jobs[key] = {'state': 'ERROR', 'progress': 0, 'error': str(exc)}
        finally:
            temporary.unlink(missing_ok=True)

    def chart(self, path, windows=None, start=None, end=None, limit=3000, mode='all'):
        if mode not in ('all', 'bars', 'ticks'):
            raise ValueError('Modalità non valida')
        info = self.inspect(path)
        if not info.get('windows'):
            raise ValueError('Analisi dello storico non ancora disponibile')
        selected = [window for window in info['windows'] if not windows or window['id'] in windows]
        if not selected:
            raise ValueError('Nessuna finestra selezionata')
        full_start, full_end = min(w['start_ns'] for w in selected), max(w['end_ns'] for w in selected)
        first = max(full_start, int(start * 1e9)) if start is not None else full_start
        last = min(full_end, int(end * 1e9)) if end is not None else full_end
        if first > last:
            return {'rows': [], 'start': full_start / 1e9, 'end': full_end / 1e9, 'window_count': len(selected)}
        ids = [w['id'] for w in selected]
        placeholders = ','.join('?' for _ in ids)
        with sqlite3.connect(self._db_path(path)) as db:
            db.row_factory = sqlite3.Row
            ranges = ' OR '.join('(ns BETWEEN ? AND ?)' for window in selected)
            params = [first, last, *(value for window in selected for value in (window['start_ns'], window['end_ns']))]
            where = f'ns BETWEEN ? AND ? AND ({ranges})'
            if mode == 'bars':
                where += " AND kind='bar' AND timeframe='intraday'"
            elif mode == 'ticks':
                where += " AND kind IN ('quote','trade')"
            total = db.execute('SELECT COUNT(*) FROM events WHERE ' + where, params).fetchone()[0]
            if mode == 'bars':
                if total > 20000:
                    raise ValueError('Oltre 20.000 barre native: seleziona un periodo più breve.')
                rows = [dict(row) for row in db.execute('SELECT * FROM events WHERE ' + where + ' ORDER BY ns,id', params)]
                resolution = 60
            elif last - first <= 3600 * 1e9:
                step = max(1, math.ceil(total / limit))
                rows = [dict(row) for index, row in enumerate(db.execute('SELECT * FROM events WHERE ' + where + ' ORDER BY ns,id', params)) if index % step == 0 or index == total - 1]
                resolution = 0
            else:
                budget = max(1, limit // 6)
                for level in LEVELS:
                    overview = list(db.execute(f'SELECT * FROM overview WHERE level=? AND window_id IN ({placeholders}) AND bucket BETWEEN ? AND ? {"AND kind IN ('quote','trade')" if mode == 'ticks' else ''} ORDER BY bucket',
                                               [level, *ids, first // (level * 1_000_000_000), last // (level * 1_000_000_000)]))
                    if len(overview) <= budget:
                        break
                step = max(1, math.ceil(len(overview) / budget))
                sampled = [row for index, row in enumerate(overview) if index % step == 0 or index == len(overview) - 1]
                event_ids = sorted({row[key] for row in sampled for key in ('first_id', 'last_id', 'min_id', 'max_id', 'ask_min_id', 'ask_max_id')})
                rows = [dict(row) for row in db.execute('SELECT * FROM events WHERE id IN (' + ','.join(str(int(value)) for value in event_ids) + ') AND ns BETWEEN ? AND ? ORDER BY ns,id', (first, last))] if event_ids else []
                resolution = level
        for row in rows:
            row.update(time=row['ns'] / 1e9, timestamp=datetime.fromtimestamp(row['ns'] / 1e9, timezone.utc).isoformat(),
                       event_type=row.pop('kind'), bid_price=row.pop('bid'), ask_price=row.pop('ask'))
            row.pop('ns')
        return {'rows': rows, 'start': full_start / 1e9, 'end': full_end / 1e9,
                'window_count': len(selected), 'events': sum(w['event_count'] for w in selected),
                'sampled': total > len(rows), 'visible_events': total, 'mode': mode, 'resolution_seconds': resolution,
                'stale': info.get('stale', False)}


archive_chart_service = ArchiveChartService()
