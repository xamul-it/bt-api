"""Observation only: persistent subscriptions and bounded access to recorded L1."""
import json
import os
import re
import tempfile
import time
import fcntl
from contextlib import contextmanager
from datetime import date, datetime, time as day_time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Blueprint, jsonify, request, send_file

from app.paths import WORKSPACE_ROOT


quote_monitor_bp = Blueprint("quote_monitor", __name__)
CONFIG = Path(os.getenv("BT_QUOTE_MONITOR_CONFIG", WORKSPACE_ROOT / "config-common/quote-monitor.json"))
STATUS = Path(os.getenv("BT_QUOTE_MONITOR_STATUS", WORKSPACE_ROOT / "out/quote-monitor-status.json"))
DUMP = Path(os.getenv("ZMQ_LOG_OUTPUT_DIR", WORKSPACE_ROOT / "out/dump"))


def valid_symbol(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", value) is not None


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def effective_symbols(config):
    return list(dict.fromkeys([*config.get('symbols', []),
                              *(symbol for group in config.get('sources', {}).values() for symbol in group)]))


@contextmanager
def config_lock():
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def save_config(symbols, source=None):
    with config_lock():
        payload = read_json(CONFIG, {'symbols': []})
        if source:
            payload.setdefault('sources', {})[source] = list(dict.fromkeys(symbols))
        else:
            payload['symbols'] = list(dict.fromkeys(symbols))
        if len(effective_symbols(payload)) > 100:
            raise ValueError('Massimo 100 simboli complessivi tra tutte le applicazioni')
        payload['updated_at'] = time.time()
        with tempfile.NamedTemporaryFile(mode='w', dir=CONFIG.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle)
        try:
            temporary.replace(CONFIG)
        finally:
            temporary.unlink(missing_ok=True)
        return payload


@quote_monitor_bp.route('/config', methods=['GET', 'PUT'])
def config():
    if request.method == 'GET':
        return jsonify(read_json(CONFIG, {"symbols": []}))
    body = request.get_json(silent=True)
    symbols = body.get('symbols') if isinstance(body, dict) else None
    if (not isinstance(symbols, list) or len(symbols) > 100
            or any(not valid_symbol(symbol) for symbol in symbols)):
        return jsonify(error="Inserisci al massimo 100 simboli azionari validi, in maiuscolo"), 400
    try:
        payload = save_config(symbols)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(payload)


@quote_monitor_bp.get('/status')
def status():
    state = read_json(STATUS, {})
    state.pop('events', None)
    has_snapshot = bool(state.get('updated_at'))
    age = time.time() - state.get('updated_at', 0)
    state['logger_alive'] = 0 <= age < 20
    state['status_age_seconds'] = max(0, age) if has_snapshot else None
    config = read_json(CONFIG, {"symbols": []})
    state['desired_symbols'] = effective_symbols(config)
    state['manual_symbols'] = config.get('symbols', [])
    state['sources'] = config.get('sources', {})
    # Presence of a file proves recorded history, not an active subscription.
    state['recordings'] = [{"symbol": symbol, "bytes": path.stat().st_size,
                            "modified_at": path.stat().st_mtime}
                           for symbol in state['desired_symbols']
                           for path in [DUMP / f'{symbol}.json'] if path.is_file()]
    return jsonify(state)


@quote_monitor_bp.get('/recordings/<symbol>')
def recordings(symbol):
    try:
        path = archive_path(symbol)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    if not path.is_file():
        return jsonify(rows=[], truncated=False)
    # Existing recordings can be gigabytes long: never scan an entire file.
    with path.open('rb') as handle:
        handle.seek(0, 2)
        size = handle.tell()
        offset = max(0, size - 2_000_000)
        handle.seek(offset)
        if offset:
            handle.readline()
        lines = handle.read(2_000_000).splitlines()
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
        except (ValueError, UnicodeDecodeError):
            continue
    return jsonify(rows=rows[-500:], truncated=bool(offset or len(rows) > 500),
                   bytes=size, scope="Ultimi 500 eventi nel segmento finale di 2 MB")


@quote_monitor_bp.get('/history/<symbol>')
def history(symbol):
    """One explicit SIP page per call; continuation is never silently discarded."""
    from app.live import _resolve_portfolio_credentials
    import requests

    if not valid_symbol(symbol) or not request.args.get('portfolio_key_id'):
        return jsonify(error="Simbolo e conto Alpaca obbligatori"), 400
    try:
        day = date.fromisoformat(request.args.get('date', ''))
    except ValueError:
        return jsonify(error="Data non valida"), 400
    ny = ZoneInfo('America/New_York')
    start = datetime.combine(day, day_time(16), ny).astimezone(timezone.utc)
    end = datetime.combine(day, day_time(20), ny).astimezone(timezone.utc)
    if end.timestamp() > time.time() - 900:
        return jsonify(error="Scegli una sessione AH conclusa da almeno 15 minuti"), 400
    key, secret, _, _, _ = _resolve_portfolio_credentials(request.args['portfolio_key_id'])
    if not key or not secret:
        return jsonify(error="Credenziali dati non disponibili"), 503
    params = {"symbols": symbol, "feed": "sip", "start": start.isoformat(),
              "end": end.isoformat(), "limit": 10000, "sort": "asc"}
    token = request.args.get('page_token')
    if token:
        if len(token) > 2048:
            return jsonify(error="Token non valido"), 400
        params['page_token'] = token
    try:
        response = requests.get('https://data.alpaca.markets/v2/stocks/quotes',
                                headers={'APCA-API-KEY-ID': key, 'APCA-API-SECRET-KEY': secret},
                                params=params, timeout=30)
        if response.status_code != 200:
            return jsonify(error=f"Download Alpaca non riuscito (HTTP {response.status_code})"), 502
        payload = response.json()
    except (requests.RequestException, ValueError):
        return jsonify(error="Servizio storico Alpaca non disponibile"), 502
    return jsonify(symbol=symbol, date=day.isoformat(), feed='sip',
                   session='16:00–20:00 America/New_York',
                   rows=(payload.get('quotes') or {}).get(symbol, []),
                   next_page_token=payload.get('next_page_token'))


@quote_monitor_bp.route('/registrations/watchtower', methods=['GET', 'PUT'])
def watchtower_registrations():
    if request.method == 'GET':
        payload = read_json(CONFIG, {'symbols': []})
        return jsonify(symbols=payload.get('sources', {}).get('watchtower', []))
    body = request.get_json(silent=True)
    symbols = body.get('symbols') if isinstance(body, dict) else None
    if not isinstance(symbols, list) or len(symbols) > 100 or any(not valid_symbol(s) for s in symbols):
        return jsonify(error='Lista simboli Watchtower non valida'), 400
    try:
        payload = save_config(symbols, source='watchtower')
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(symbols=payload['sources']['watchtower'], effective_symbols=effective_symbols(payload))


_services_cache = {'at': 0, 'rows': []}


@quote_monitor_bp.get('/admin')
def admin():
    from app.watchtower import _systemctl_show
    now = time.time()
    if now - _services_cache['at'] > 5:
        try:
            _services_cache['rows'] = [_systemctl_show(unit) for unit in ('zmq-proxy.service', 'zmq-logger.service')]
            _services_cache['at'] = now
        except Exception:
            _services_cache['rows'] = []
    state = read_json(STATUS, {})
    state.pop('events', None)
    state['fresh'] = 0 <= now - state.get('updated_at', 0) < 20
    state['services'] = _services_cache['rows']
    config = read_json(CONFIG, {'symbols': []})
    state['registrations'] = {'manuale': config.get('symbols', []), **config.get('sources', {})}
    return jsonify(state)


@quote_monitor_bp.get('/live')
def live():
    try:
        after = int(request.args.get('after', 0))
        if after < 0:
            raise ValueError()
    except ValueError:
        return jsonify(error='Cursore non valido'), 400
    symbol = request.args.get('symbol')
    if symbol and not valid_symbol(symbol):
        return jsonify(error='Simbolo non valido'), 400
    state = read_json(STATUS, {})
    events = state.get('events', [])
    session = request.args.get('session')
    reset = bool(session and session != state.get('session_id'))
    if reset:
        after = 0
    gap = bool(after and events and after < events[0]['sequence'] - 1)
    return jsonify(session_id=state.get('session_id'), sequence=state.get('sequence', 0),
                   updated_at=state.get('updated_at'), gap=gap, reset=reset,
                   rows=[row for row in events if row['sequence'] > after and (not symbol or row.get('symbol') == symbol)])


def archive_path(symbol):
    if not valid_symbol(symbol):
        raise ValueError('Simbolo non valido')
    path = DUMP / f'{symbol}.json'
    if path.is_symlink() or path.resolve().parent != DUMP.resolve():
        raise ValueError('Percorso archivio non valido')
    return path


def archive_version(path):
    stat = path.stat()
    return f'{stat.st_size}:{stat.st_mtime_ns}'


@quote_monitor_bp.get('/archives')
def archives():
    rows = []
    for path in DUMP.glob('*.json'):
        if not valid_symbol(path.stem) or path.is_symlink() or not path.is_file():
            continue
        stat = path.stat()
        rows.append({'symbol': path.stem, 'bytes': stat.st_size, 'modified_at': stat.st_mtime,
                     'version': f'{stat.st_size}:{stat.st_mtime_ns}'})
    return jsonify(rows=sorted(rows, key=lambda row: row['symbol']))


@quote_monitor_bp.get('/archives/<symbol>/events')
def archive_events(symbol):
    try:
        path = archive_path(symbol)
        offset = int(request.args.get('offset', 0))
        if offset < 0:
            raise ValueError('Offset non valido')
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    if not path.is_file():
        return jsonify(error='Registrazione non trovata'), 404
    rows = []
    with path.open('rb') as handle:
        size = path.stat().st_size
        if offset > size:
            return jsonify(error='Offset oltre la dimensione del file'), 409
        handle.seek(offset)
        # An incomplete final line is retried on the next call, never consumed.
        while len(rows) < 1000 and handle.tell() - offset < 1_000_000:
            position = handle.tell()
            line = handle.readline(1_000_001)
            if len(line) > 1_000_000:
                return jsonify(error='Riga archivio troppo lunga: consultazione interrotta'), 422
            if not line or not line.endswith(b'\n'):
                handle.seek(position)
                break
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)
            except (ValueError, UnicodeDecodeError):
                continue
        next_offset = handle.tell()
    return jsonify(rows=rows, next_offset=next_offset, bytes=size, complete=next_offset >= size,
                   waiting_for_line=next_offset < size and not rows)


@quote_monitor_bp.get('/archives/<symbol>/download')
def archive_download(symbol):
    try:
        path = archive_path(symbol)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    if not path.is_file():
        return jsonify(error='Registrazione non trovata'), 404
    return send_file(path, as_attachment=True, download_name=f'{symbol}.jsonl', conditional=True)


@quote_monitor_bp.delete('/archives/<symbol>')
def delete_archive(symbol):
    from app.service.replay_service import replay_service
    try:
        path = archive_path(symbol)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict) or body.get('confirm_symbol') != symbol:
        return jsonify(error='Conferma digitando il simbolo del file da cancellare'), 400
    # Serialize against new saved subscriptions; also check actual clients, not only the saved list.
    with config_lock():
        replay = replay_service.status()
        if replay.get('running') and symbol in replay.get('requested_symbols', []):
            return jsonify(error='Archivio in uso dal replay: arresta prima il proxy replay'), 409
        state = read_json(STATUS, {})
        if not (0 <= time.time() - state.get('updated_at', 0) < 20 and state.get('proxy_connected')):
            return jsonify(error='Impossibile verificare le sottoscrizioni attive: riprova con il monitor raggiungibile'), 409
        active = effective_symbols(read_json(CONFIG, {'symbols': []}))
        active += [item['symbol'] for client in state.get('proxy', {}).get('clients', []) for item in client.get('subscriptions', [])]
        if symbol in active:
            return jsonify(error='Flusso ancora sottoscritto: rimuovi prima il simbolo dalle applicazioni che lo richiedono'), 409
        if not path.is_file():
            return jsonify(error='Registrazione non trovata'), 404
        if body.get('version') != archive_version(path):
            return jsonify(error='Il file è cambiato: aggiorna la lista prima di cancellare'), 409
        path.unlink()
    return jsonify(deleted=True, symbol=symbol)


@quote_monitor_bp.route('/replay', methods=['GET', 'POST', 'DELETE'])
def replay_session():
    from app.service.replay_service import replay_service, ReplayConflict
    import math
    if request.method == 'GET':
        return jsonify(replay_service.status())
    if request.method == 'DELETE':
        return jsonify(replay_service.stop())
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error='Configurazione replay obbligatoria'), 400
    symbols = body.get('symbols')
    if (not isinstance(symbols, list) or not symbols or len(symbols) > 20
            or any(not valid_symbol(s) for s in symbols)):
        return jsonify(error='Scegli da 1 a 20 simboli validi'), 400
    try:
        replay_date = date.fromisoformat(body.get('date', ''))
        speed = float(body.get('speed', 10))
        if not math.isfinite(speed) or not 0 <= speed <= 10000:
            raise ValueError('Velocità: da 0 (massima) a 10000')
        session = body.get('session', 'all')
        if session not in ('all', 'rth', 'ah'):
            raise ValueError('Sessione non valida')
        remap = body.get('remap_today', False)
        if not isinstance(remap, bool):
            raise ValueError('Scelta timestamp non valida')
        for symbol in symbols:
            if not archive_path(symbol).is_file():
                raise ValueError(f'Archivio non disponibile per {symbol}')
        state = replay_service.prepare(list(dict.fromkeys(symbols)), replay_date.isoformat(), speed, session, DUMP, remap)
    except ReplayConflict as exc:
        return jsonify(error=str(exc)), 409
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400
    except OSError:
        return jsonify(error='Impossibile avviare il processo replay'), 503
    return jsonify(state), 202


@quote_monitor_bp.post('/replay/control')
def replay_control():
    from app.service.replay_service import replay_service, ReplayConflict
    body = request.get_json(silent=True)
    action = body.get('action') if isinstance(body, dict) else None
    try:
        result = replay_service.control(action)
    except ReplayConflict as exc:
        return jsonify(error=str(exc)), 409
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(result)
