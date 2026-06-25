# Crea un Blueprint per il controller dello scheduler
import logging
from datetime import datetime, timezone
from flask import Blueprint, jsonify, request
#from alpaca_trade_api import REST
from app.manager.cerebro_manager import CerebroManager
from app.paths import CONFIG_PATH

from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, OrderType, TimeInForce
import os
import requests
import json
from pathlib import Path
from urllib.parse import urlsplit
from app.service.main_service import repo


al_bp = Blueprint('live', __name__)
logger = logging.getLogger(__name__)

BASE_URL = 'https://paper-api.alpaca.markets'

# SSL verification configuration (for corporate proxies like Zscaler)
# Set DISABLE_SSL_VERIFY=true only if you have SSL interception issues
# WARNING: Disabling SSL verification exposes you to MITM attacks
DISABLE_SSL_VERIFY = os.environ.get('DISABLE_SSL_VERIFY', 'false').lower() in ('true', '1', 'yes')
ALPACA_CACHE_DIR = Path(CONFIG_PATH) / 'alpaca_cache'
ALPACA_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Security: Origin validation for live trading endpoints
# Set ALLOWED_ORIGINS in production, leave unset for development
ALLOWED_ORIGINS = os.environ.get('ALLOWED_ORIGINS', None)
ENABLE_ORIGIN_CHECK = ALLOWED_ORIGINS is not None

if not ENABLE_ORIGIN_CHECK:
    logger.warning("Origin checking DISABLED for live trading endpoints - only use in development!")
else:
    allowed_list = [o.strip() for o in ALLOWED_ORIGINS.split(',')]
    logger.info(f"Origin checking ENABLED for live trading. Allowed: {allowed_list}")


def _origin_matches(candidate, allowed):
    if not candidate:
        return False
    if allowed.endswith(':*'):
        candidate_parts = urlsplit(candidate)
        allowed_parts = urlsplit(allowed[:-2])
        return (
            candidate_parts.scheme == allowed_parts.scheme
            and candidate_parts.hostname == allowed_parts.hostname
        )
    return candidate == allowed


def _iso_or_none(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _resolve_portfolio_credentials(portfolio_key_id=None):
    resolved = str(portfolio_key_id or "").strip() or None
    if repo.available():
        try:
            portfolio = repo.resolve_alpaca_portfolio(resolved)
            return (
                portfolio.get('alpaca_api_key'),
                portfolio.get('alpaca_secret_key'),
                bool(portfolio.get('paper', True)),
                portfolio.get('portfolio_key_id'),
                portfolio.get('display_name'),
            )
        except Exception:
            logger.exception("Unable to resolve Alpaca portfolio %s", resolved)
    api_key = os.environ.get('ALPACA_API_KEY', '')
    secret_key = os.environ.get('ALPACA_SECRET_KEY', '')
    return (api_key, secret_key, True, resolved or 'legacy/default', None)


def _alpaca_get(path, params=None, portfolio_key_id=None):
    api_key, secret_key, _paper, _resolved_key, _display_name = _resolve_portfolio_credentials(portfolio_key_id)
    if not api_key or not secret_key:
        return None, ('Alpaca credentials not configured', 503)

    headers = {
        'APCA-API-KEY-ID': api_key,
        'APCA-API-SECRET-KEY': secret_key,
    }
    url = f"{BASE_URL}{path}"

    try:
        resp = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=20,
            verify=not DISABLE_SSL_VERIFY,
        )
    except Exception as exc:
        logger.error("Error contacting Alpaca endpoint %s: %s", path, exc)
        return None, (str(exc), 500)

    if resp.status_code >= 400:
        logger.warning("Alpaca endpoint %s failed: %s - %s", path, resp.status_code, resp.text)
        try:
            payload = resp.json()
            message = payload.get('message', resp.text)
        except Exception:
            message = resp.text
        return None, (message, resp.status_code)

    try:
        return resp.json(), None
    except ValueError:
        return None, ('Invalid JSON from Alpaca', 502)


def _to_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def _load_json(path, default):
    try:
        with open(path, 'r') as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except Exception:
        logger.exception("Unable to load cache file %s", path)
        return default


def _save_json(path, payload):
    tmp_path = f"{path}.tmp"
    with open(tmp_path, 'w') as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def _portfolio_history_cache_path(portfolio_key_id, timeframe, intraday_reporting):
    safe_reporting = str(intraday_reporting or 'market_hours').replace('/', '_')
    safe_timeframe = str(timeframe or '1Min').replace('/', '_')
    safe_portfolio = str(portfolio_key_id or 'legacy_default').replace('/', '_')
    return ALPACA_CACHE_DIR / f'portfolio_history__{safe_portfolio}__{safe_timeframe}__{safe_reporting}.json'


def _activities_cache_path(portfolio_key_id):
    safe_portfolio = str(portfolio_key_id or 'legacy_default').replace('/', '_')
    return ALPACA_CACHE_DIR / f'activities__{safe_portfolio}.json'


def _merge_history_points(cache_payload, points, timeframe, intraday_reporting, base_value):
    by_timestamp = {
        point['timestamp']: point
        for point in cache_payload.get('points', [])
        if point.get('timestamp')
    }
    for point in points:
        timestamp = point.get('timestamp')
        if timestamp:
            by_timestamp[timestamp] = point

    merged_points = [by_timestamp[key] for key in sorted(by_timestamp.keys())]
    return {
        'timeframe': timeframe,
        'intraday_reporting': intraday_reporting,
        'base_value': base_value,
        'points': merged_points,
        'updated_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
    }


def _filter_history_points(points, start_iso=None, end_iso=None):
    filtered = []
    start_ts = datetime.fromisoformat(start_iso.replace('Z', '+00:00')) if start_iso else None
    end_ts = datetime.fromisoformat(end_iso.replace('Z', '+00:00')) if end_iso else None

    for point in points:
        timestamp = point.get('timestamp')
        if not timestamp:
            continue
        point_ts = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        if start_ts and point_ts < start_ts:
            continue
        if end_ts and point_ts > end_ts:
            continue
        filtered.append(point)
    return filtered


def _history_response(points, timeframe, base_value, cache_metadata=None):
    raw_timestamps = []
    raw_equity = []
    raw_profit_loss = []
    raw_profit_loss_pct = []

    for point in points:
        timestamp = point.get('timestamp')
        if timestamp:
            raw_timestamps.append(int(datetime.fromisoformat(timestamp.replace('Z', '+00:00')).timestamp()))
        raw_equity.append(point.get('equity'))
        raw_profit_loss.append(point.get('profit_loss'))
        raw_profit_loss_pct.append(point.get('profit_loss_pct'))

    return {
        'timeframe': timeframe,
        'base_value': base_value,
        'currency': 'USD',
        'points': points,
        'raw': {
            'timestamp': raw_timestamps,
            'equity': raw_equity,
            'profit_loss': raw_profit_loss,
            'profit_loss_pct': raw_profit_loss_pct,
        },
        'cache': cache_metadata or {},
    }


def _upsert_activities_cache(activities, portfolio_key_id):
    cache_path = _activities_cache_path(portfolio_key_id)
    cache_payload = _load_json(cache_path, {'activities': [], 'updated_at': None})
    by_id = {
        activity['id']: activity
        for activity in cache_payload.get('activities', [])
        if activity.get('id')
    }
    for activity in activities:
        activity_id = activity.get('id')
        if activity_id:
            by_id[activity_id] = activity

    merged = [by_id[key] for key in sorted(by_id.keys())]
    payload = {
        'activities': merged,
        'updated_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
    }
    _save_json(cache_path, payload)
    return payload


def _filter_cached_activities(cached_activities, params, symbol_filter=None):
    activity_types = [t.strip() for t in str(params.get('activity_types', 'FILL')).split(',') if t.strip()]
    after = params.get('after')
    until = params.get('until')
    date = params.get('date')

    after_ts = datetime.fromisoformat(after.replace('Z', '+00:00')) if after else None
    until_ts = datetime.fromisoformat(until.replace('Z', '+00:00')) if until else None

    filtered = []
    for activity in cached_activities:
        if activity_types and activity.get('activity_type') not in activity_types:
            continue
        if symbol_filter and activity.get('symbol') != symbol_filter:
            continue

        timestamp = activity.get('timestamp')
        if timestamp:
            point_ts = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            if after_ts and point_ts < after_ts:
                continue
            if until_ts and point_ts > until_ts:
                continue
            if date and timestamp[:10] != date[:10]:
                continue

        filtered.append(activity)
    return filtered


@al_bp.before_request
def check_origin():
    """
    Validate request origin for live trading endpoints.
    Only enforced when ALLOWED_ORIGINS environment variable is set.
    """
    if not ENABLE_ORIGIN_CHECK:
        return  # Skip check in development

    # Check Origin header (for CORS requests)
    origin = request.headers.get('Origin')
    referer = request.headers.get('Referer')

    allowed_list = [o.strip() for o in ALLOWED_ORIGINS.split(',')]

    # Validate origin or referer
    is_valid = False
    if origin and any(_origin_matches(origin, allowed) for allowed in allowed_list):
        is_valid = True
    elif referer and any(_origin_matches(referer, allowed) for allowed in allowed_list):
        is_valid = True

    if not is_valid:
        logger.warning(
            f"Blocked request to {request.path} from origin={origin}, "
            f"referer={referer}, ip={request.remote_addr}"
        )
        from flask import jsonify
        return jsonify({'error': 'Forbidden - Invalid origin'}), 403

def _trading_clients(portfolio_key_id=None):
    api_key, secret_key, paper, resolved_key, _display_name = _resolve_portfolio_credentials(portfolio_key_id)
    if not api_key or not secret_key:
        return None, None, resolved_key
    trading_client = TradingClient(api_key, secret_key, paper=paper)
    historical_client = StockHistoricalDataClient(api_key, secret_key)
    if DISABLE_SSL_VERIFY:
        logger.warning("SSL verification DISABLED for Alpaca clients - only use with corporate proxies!")
        trading_client._session.verify = False
        historical_client._session.verify = False
    return trading_client, historical_client, resolved_key


@al_bp.route('/portfolio', methods=['GET'])
def get_portfolio():
    portfolio_key_id = request.args.get('portfolio_key_id')
    trading_client, _historical_client, resolved_key = _trading_clients(portfolio_key_id)
    if trading_client is None:
        return jsonify({'error': 'Alpaca credentials not configured'}), 503
    try:
        account = trading_client.get_account()
        positions = trading_client.get_all_positions()
        return jsonify({
            'portfolio_key_id': resolved_key,
            'account': account.dict(),
            'positions': [position.dict() for position in positions]
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@al_bp.route('/portfolio-history', methods=['GET'])
def get_portfolio_history():
    requested_portfolio_key_id = request.args.get('portfolio_key_id')
    _api_key, _secret_key, _paper, resolved_portfolio_key_id, _display_name = _resolve_portfolio_credentials(requested_portfolio_key_id)
    params = {
        'period': request.args.get('period', '1W'),
        'timeframe': request.args.get('timeframe', '1Min'),
        'intraday_reporting': request.args.get('intraday_reporting', 'market_hours'),
        'pnl_reset': request.args.get('pnl_reset', 'per_day'),
        'extended_hours': request.args.get('extended_hours', 'false').lower(),
    }
    prefer_cache = _to_bool(request.args.get('prefer_cache'), default=False)

    start = request.args.get('start') or request.args.get('date_start')
    normalized_start = _iso_or_none(start)
    if start and not normalized_start:
        return jsonify({'error': 'Invalid start. Use ISO8601 format.'}), 400
    if normalized_start:
        params['start'] = normalized_start

    end = request.args.get('end') or request.args.get('date_end')
    normalized_end = _iso_or_none(end)
    if end and not normalized_end:
        return jsonify({'error': 'Invalid end. Use ISO8601 format.'}), 400
    if normalized_end:
        params['end'] = normalized_end

    if normalized_start and normalized_end:
        params.pop('period', None)

    cache_path = _portfolio_history_cache_path(resolved_portfolio_key_id, params['timeframe'], params['intraday_reporting'])
    cache_payload = _load_json(cache_path, {
        'timeframe': params['timeframe'],
        'intraday_reporting': params['intraday_reporting'],
        'base_value': None,
        'points': [],
        'updated_at': None,
    })

    if prefer_cache and cache_payload.get('points'):
        cached_points = _filter_history_points(
            cache_payload.get('points', []),
            start_iso=normalized_start,
            end_iso=normalized_end,
        )
        if cached_points:
            return jsonify(_history_response(
                cached_points,
                cache_payload.get('timeframe'),
                cache_payload.get('base_value'),
                cache_metadata={
                    'mode': 'local',
                    'path': str(cache_path),
                    'updated_at': cache_payload.get('updated_at'),
                    'points': len(cached_points),
                }
            ))

    payload, error = _alpaca_get('/v2/account/portfolio/history', params=params, portfolio_key_id=resolved_portfolio_key_id)
    if error:
        cached_points = _filter_history_points(
            cache_payload.get('points', []),
            start_iso=normalized_start,
            end_iso=normalized_end,
        )
        if cached_points:
            return jsonify(_history_response(
                cached_points,
                cache_payload.get('timeframe'),
                cache_payload.get('base_value'),
                cache_metadata={
                    'mode': 'local-fallback',
                    'path': str(cache_path),
                    'updated_at': cache_payload.get('updated_at'),
                    'points': len(cached_points),
                    'remote_error': error[0],
                }
            ))
        return jsonify({'error': error[0]}), error[1]

    timestamps = payload.get('timestamp', [])
    equity = payload.get('equity', [])
    profit_loss = payload.get('profit_loss', [])
    profit_loss_pct = payload.get('profit_loss_pct', [])
    base_value = payload.get('base_value')

    points = []
    for idx, ts in enumerate(timestamps):
        iso_ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace('+00:00', 'Z')
        points.append({
            'timestamp': iso_ts,
            'equity': equity[idx] if idx < len(equity) else None,
            'profit_loss': profit_loss[idx] if idx < len(profit_loss) else None,
            'profit_loss_pct': profit_loss_pct[idx] if idx < len(profit_loss_pct) else None,
        })

    merged_cache = _merge_history_points(
        cache_payload,
        points,
        payload.get('timeframe') or params['timeframe'],
        params['intraday_reporting'],
        base_value,
    )
    _save_json(cache_path, merged_cache)

    return jsonify(_history_response(
        points,
        payload.get('timeframe'),
        base_value,
        cache_metadata={
            'portfolio_key_id': resolved_portfolio_key_id,
            'mode': 'remote+cached',
            'path': str(cache_path),
            'updated_at': merged_cache.get('updated_at'),
            'points': len(points),
            'stored_points': len(merged_cache.get('points', [])),
        }
    ))


@al_bp.route('/activities', methods=['GET'])
def get_activities():
    requested_portfolio_key_id = request.args.get('portfolio_key_id')
    _api_key, _secret_key, _paper, resolved_portfolio_key_id, _display_name = _resolve_portfolio_credentials(requested_portfolio_key_id)
    page_size = min(int(request.args.get('page_size', '100')), 100)
    fetch_all = _to_bool(request.args.get('fetch_all'), default=True)
    max_pages = max(1, min(int(request.args.get('max_pages', '20')), 100))
    params = {
        'activity_types': request.args.get('activity_types', 'FILL'),
        'direction': request.args.get('direction', 'desc'),
        'page_size': page_size,
    }

    date = request.args.get('date')
    if date:
        normalized_date = _iso_or_none(date)
        if not normalized_date:
            return jsonify({'error': 'Invalid date. Use ISO8601 format.'}), 400
        params['date'] = normalized_date

    after = request.args.get('after')
    if after:
        normalized_after = _iso_or_none(after)
        if not normalized_after:
            return jsonify({'error': 'Invalid after. Use ISO8601 format.'}), 400
        params['after'] = normalized_after

    until = request.args.get('until')
    if until:
        normalized_until = _iso_or_none(until)
        if not normalized_until:
            return jsonify({'error': 'Invalid until. Use ISO8601 format.'}), 400
        params['until'] = normalized_until

    page_token = request.args.get('page_token')
    if page_token:
        params['page_token'] = page_token

    symbol_filter = request.args.get('symbol')
    prefer_cache = _to_bool(request.args.get('prefer_cache'), default=False)
    activities = []
    pages_loaded = 0
    next_page_token = params.get('page_token')
    truncated = False
    cache_path = _activities_cache_path(resolved_portfolio_key_id)
    cache_payload = _load_json(cache_path, {'activities': [], 'updated_at': None})

    if prefer_cache and cache_payload.get('activities'):
        activities = _filter_cached_activities(cache_payload.get('activities', []), params, symbol_filter=symbol_filter)
        return jsonify({
            'activities': activities,
            'count': len(activities),
            'pages_loaded': 0,
            'page_size': page_size,
            'fetch_all': fetch_all,
            'truncated': False,
            'next_page_token': None,
            'cache': {
                'mode': 'local',
                'path': str(cache_path),
                'updated_at': cache_payload.get('updated_at'),
            },
            'filters': {
                'activity_types': params['activity_types'],
                'direction': params['direction'],
                'symbol': symbol_filter,
            }
        })

    while True:
        payload, error = _alpaca_get('/v2/account/activities', params=params, portfolio_key_id=resolved_portfolio_key_id)
        if error:
            cached_activities = _filter_cached_activities(cache_payload.get('activities', []), params, symbol_filter=symbol_filter)
            if cached_activities:
                return jsonify({
                    'activities': cached_activities,
                    'count': len(cached_activities),
                    'pages_loaded': pages_loaded,
                    'page_size': page_size,
                    'fetch_all': fetch_all,
                    'truncated': False,
                    'next_page_token': None,
                    'cache': {
                        'mode': 'local-fallback',
                        'path': str(cache_path),
                        'updated_at': cache_payload.get('updated_at'),
                        'remote_error': error[0],
                    },
                    'filters': {
                        'activity_types': params['activity_types'],
                        'direction': params['direction'],
                        'symbol': symbol_filter,
                    }
                })
            return jsonify({'error': error[0]}), error[1]

        page_items = payload if isinstance(payload, list) else []
        pages_loaded += 1

        for activity in page_items:
            symbol = activity.get('symbol')
            if symbol_filter and symbol != symbol_filter:
                continue

            qty = activity.get('qty')
            price = activity.get('price')
            side = activity.get('side')
            transaction_time = _iso_or_none(activity.get('transaction_time'))

            activities.append({
                'id': activity.get('id'),
                'activity_type': activity.get('activity_type'),
                'symbol': symbol,
                'side': side,
                'qty': float(qty) if qty is not None else None,
                'price': float(price) if price is not None else None,
                'notional': float(activity.get('net_amount')) if activity.get('net_amount') is not None else None,
                'timestamp': transaction_time,
                'order_id': activity.get('order_id'),
                'raw': activity,
            })

        if not fetch_all or len(page_items) < page_size:
            next_page_token = None
            break

        if pages_loaded >= max_pages:
            next_page_token = page_items[-1].get('id') if page_items else None
            truncated = bool(next_page_token)
            break

        next_page_token = page_items[-1].get('id') if page_items else None
        if not next_page_token:
            break
        params['page_token'] = next_page_token

    merged_cache = _upsert_activities_cache(activities, resolved_portfolio_key_id)

    return jsonify({
        'activities': activities,
        'count': len(activities),
        'pages_loaded': pages_loaded,
        'page_size': page_size,
        'fetch_all': fetch_all,
        'truncated': truncated,
        'next_page_token': next_page_token if truncated else None,
        'cache': {
            'portfolio_key_id': resolved_portfolio_key_id,
            'mode': 'remote+cached',
            'path': str(cache_path),
            'updated_at': merged_cache.get('updated_at'),
            'stored_activities': len(merged_cache.get('activities', [])),
        },
        'filters': {
            'activity_types': params['activity_types'],
            'direction': params['direction'],
            'symbol': symbol_filter,
        }
    })


@al_bp.route('/order', methods=['POST'])
def create_order():
    trading_client, _historical_client, _resolved_key = _trading_clients(request.args.get('portfolio_key_id'))
    if trading_client is None:
        return jsonify({'error': 'Alpaca credentials not configured'}), 503
    try:
        data = request.json
        order_request = MarketOrderRequest(
            symbol=data['symbol'],
            qty=data['qty'],
            side=OrderSide.BUY if data['side'] == 'buy' else OrderSide.SELL,
            type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY
        )
        order = trading_client.submit_order(order_request)
        return jsonify(order.dict())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@al_bp.route('/alpaca-tickers', methods=['GET'])
def get_alpaca_tickers():
    trading_client, _historical_client, _resolved_key = _trading_clients(request.args.get('portfolio_key_id'))
    if trading_client is None:
        return jsonify({'error': 'Alpaca credentials not configured'}), 503
    try:
        # Ottieni tutti gli asset disponibili su Alpaca
        assets = trading_client.get_all_assets()

        # Filtra solo i simboli (ticker)
        tickers = [asset.symbol for asset in assets if asset.tradable]

        return jsonify({'tickers': tickers})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

logger = logging.getLogger(__name__)

cerebro_manager = CerebroManager()

@al_bp.route('/cerebro', methods=['POST'])
def create_cerebro_instance():
    data = request.json
    try:
        instance = cerebro_manager.create_instance(
            name=data['name'],
            feed_list=data['feed_list'],
            broker_name=data['broker_name'],
            strategy_name=data['strategy_name']
        )
        return jsonify({
            "message": "Cerebro instance created",
            "instance": {
                "name": instance.name,
                "status": instance.status,
            }
        }), 201
    except Exception as e:
        logger.error(f"Error creating Cerebro instance: {e}")
        return jsonify({"error": str(e)}), 400

@al_bp.route('/cerebro/<name>/start', methods=['POST'])
def start_cerebro_instance(name):
    try:
        cerebro_manager.start_instance(name)
        return jsonify({"message": f"Cerebro instance {name} started"}), 200
    except Exception as e:
        logger.error(f"Error starting Cerebro instance {name}: {e}")
        return jsonify({"error": str(e)}), 400

@al_bp.route('/cerebro/<name>/stop', methods=['POST'])
def stop_cerebro_instance(name):
    try:
        cerebro_manager.stop_instance(name)
        return jsonify({"message": f"Cerebro instance {name} stopped"}), 200
    except Exception as e:
        logger.error(f"Error stopping Cerebro instance {name}: {e}")
        return jsonify({"error": str(e)}), 400

@al_bp.route('/cerebro', methods=['GET'])
def list_cerebro_instances():
    try:
        instances = cerebro_manager.list_instances()
        return jsonify({
            "instances": [{
                "name": instance.name,
                "status": instance.status,
                "cash": instance.getbroker().get_cash(),
                "value": instance.getbroker().get_fundvalue(),
            } for instance in instances]
        }), 200
    except Exception as e:
        logger.error(f"Error listing Cerebro instances: {e}")
        return jsonify({"error": str(e)}), 400

@al_bp.route('/cerebro/<name>/positions', methods=['GET'])
def get_cerebro_positions(name):
    try:
        instance = cerebro_manager.get_instance(name)
        return jsonify({
            "name": instance.name,
            "positions": instance.positions
        }), 200
    except Exception as e:
        logger.error(f"Error getting positions for Cerebro instance {name}: {e}")
        return jsonify({"error": str(e)}), 400
