# Crea un Blueprint per il controller dello scheduler
import logging
from datetime import datetime, timezone
from flask import Blueprint, jsonify, request
#from alpaca_trade_api import REST
from app.manager.cerebro_manager import CerebroManager

from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, OrderType, TimeInForce
import os
import requests


al_bp = Blueprint('live', __name__)
logger = logging.getLogger(__name__)

API_KEY = os.environ.get('ALPACA_API_KEY','')
SECRET_KEY = os.environ.get('ALPACA_SECRET_KEY','')
BASE_URL = 'https://paper-api.alpaca.markets'

# SSL verification configuration (for corporate proxies like Zscaler)
# Set DISABLE_SSL_VERIFY=true only if you have SSL interception issues
# WARNING: Disabling SSL verification exposes you to MITM attacks
DISABLE_SSL_VERIFY = os.environ.get('DISABLE_SSL_VERIFY', 'false').lower() in ('true', '1', 'yes')

# Security: Origin validation for live trading endpoints
# Set ALLOWED_ORIGINS in production, leave unset for development
ALLOWED_ORIGINS = os.environ.get('ALLOWED_ORIGINS', None)
ENABLE_ORIGIN_CHECK = ALLOWED_ORIGINS is not None

if not ENABLE_ORIGIN_CHECK:
    logger.warning("Origin checking DISABLED for live trading endpoints - only use in development!")
else:
    allowed_list = [o.strip() for o in ALLOWED_ORIGINS.split(',')]
    logger.info(f"Origin checking ENABLED for live trading. Allowed: {allowed_list}")


def _iso_or_none(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _alpaca_get(path, params=None):
    if not API_KEY or not SECRET_KEY:
        return None, ('Alpaca credentials not configured', 503)

    headers = {
        'APCA-API-KEY-ID': API_KEY,
        'APCA-API-SECRET-KEY': SECRET_KEY,
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
    if origin and any(origin.startswith(allowed) for allowed in allowed_list):
        is_valid = True
    elif referer and any(referer.startswith(allowed) for allowed in allowed_list):
        is_valid = True

    if not is_valid:
        logger.warning(
            f"Blocked request to {request.path} from origin={origin}, "
            f"referer={referer}, ip={request.remote_addr}"
        )
        from flask import jsonify
        return jsonify({'error': 'Forbidden - Invalid origin'}), 403

trading_client = None
historical_client = None
if API_KEY and SECRET_KEY:
    trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
    if DISABLE_SSL_VERIFY:
        logger.warning("SSL verification DISABLED for trading_client - only use with corporate proxies!")
        trading_client._session.verify = False

    historical_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)
    if DISABLE_SSL_VERIFY:
        logger.warning("SSL verification DISABLED for historical_client - only use with corporate proxies!")
        historical_client._session.verify = False
else:
    logger.warning("ALPACA_API_KEY/ALPACA_SECRET_KEY non configurate: endpoint live non disponibili")


@al_bp.route('/portfolio', methods=['GET'])
def get_portfolio():
    if trading_client is None:
        return jsonify({'error': 'Alpaca credentials not configured'}), 503
    try:
        account = trading_client.get_account()
        positions = trading_client.get_all_positions()
        return jsonify({
            'account': account.dict(),
            'positions': [position.dict() for position in positions]
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@al_bp.route('/portfolio-history', methods=['GET'])
def get_portfolio_history():
    params = {
        'period': request.args.get('period', '1W'),
        'timeframe': request.args.get('timeframe', '1Min'),
        'intraday_reporting': request.args.get('intraday_reporting', 'market_hours'),
        'pnl_reset': request.args.get('pnl_reset', 'per_day'),
        'extended_hours': request.args.get('extended_hours', 'false').lower(),
    }

    date_end = request.args.get('date_end')
    normalized_date_end = _iso_or_none(date_end)
    if date_end and not normalized_date_end:
        return jsonify({'error': 'Invalid date_end. Use ISO8601 format.'}), 400
    if normalized_date_end:
        params['date_end'] = normalized_date_end

    payload, error = _alpaca_get('/v2/account/portfolio/history', params=params)
    if error:
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

    return jsonify({
        'timeframe': payload.get('timeframe'),
        'base_value': base_value,
        'currency': 'USD',
        'points': points,
        'raw': {
            'timestamp': timestamps,
            'equity': equity,
            'profit_loss': profit_loss,
            'profit_loss_pct': profit_loss_pct,
        },
    })


@al_bp.route('/activities', methods=['GET'])
def get_activities():
    params = {
        'activity_types': request.args.get('activity_types', 'FILL'),
        'direction': request.args.get('direction', 'desc'),
        'page_size': request.args.get('page_size', '200'),
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

    payload, error = _alpaca_get('/v2/account/activities', params=params)
    if error:
        return jsonify({'error': error[0]}), error[1]

    symbol_filter = request.args.get('symbol')
    activities = []

    for activity in payload if isinstance(payload, list) else []:
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

    return jsonify({
        'activities': activities,
        'count': len(activities),
        'filters': {
            'activity_types': params['activity_types'],
            'direction': params['direction'],
            'symbol': symbol_filter,
        }
    })


@al_bp.route('/order', methods=['POST'])
def create_order():
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
