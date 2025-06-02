# Crea un Blueprint per il controller dello scheduler
import logging
from flask import Blueprint, app, jsonify, request
#from alpaca_trade_api import REST
from app.manager.cerebro_manager import CerebroManager

from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, OrderType, TimeInForce
from flask import Blueprint, jsonify, request
from .manager.cerebro_manager import CerebroManager
from .manager.cerebro_manager import CerebroInstance
import logging
import os
import requests


al_bp = Blueprint('live', __name__)
logger = logging.getLogger(__name__)

API_KEY = os.environ.get('ALPACA_API_KEY','')
SECRET_KEY = os.environ.get('ALPACA_SECRET_KEY','')
BASE_URL = 'https://paper-api.alpaca.markets'


trading_client = TradingClient(API_KEY, API_SECRET, paper=True)
trading_client._session.verify = False

historical_client = StockHistoricalDataClient(API_KEY, API_SECRET)
historical_client._session.verify = False


@al_bp.route('/portfolio', methods=['GET'])
def get_portfolio():
    try:
        account = trading_client.get_account()
        positions = trading_client.get_all_positions()
        return jsonify({
            'account': account.dict(),
            'positions': [position.dict() for position in positions]
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@al_bp.route('/order', methods=['POST'])
def create_order():
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
    try:
        # Ottieni tutti gli asset disponibili su Alpaca
        assets = trading_client.get_all_assets()
        
        # Filtra solo i simboli (ticker)
        tickers = [asset.symbol for asset in assets if asset.tradable]
        
        return jsonify({'tickers': tickers})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

logger = logging.getLogger(__name__)

#cerebro_manager = CerebroManager()

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