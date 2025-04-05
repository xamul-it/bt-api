from dataclasses import dataclass
from datetime import datetime
from backtrader import Cerebro
import backtrader as bt
import broker.alpacaBroker as al
import broker.alpaca_data as al_data
from pytickersymbols import PyTickerSymbols
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
import pandas as pd
from alpaca.data.timeframe import TimeFrame
import logging
import threading
import logging
from pytickersymbols import PyTickerSymbols
from strategies import multiTickerStrategy as mts
from strategies import weekly
import strategies
from itertools import islice
import os

logger = logging.getLogger(__name__)
# Configurazione di base del logging
logging.basicConfig(level=logging.DEBUG)

symbol = al_data.AlpacaLiveData(
                symbol='AZN',
                api_key=os.environ['ALPACA_API_KEY'],
                secret_key=os.environ['ALPACA_SECRET_KEY'],
                timeframe=bt.TimeFrame.Minutes,
                compression=1
            )

@dataclass
class CerebroInstance:
    name: str
    feed_list: str  # Nome del file CSV con l'elenco dei simboli
    broker_name: str
    strategy_name: str
    cerebro: bt.cerebro = Cerebro()  # Istanza di Backtrader Cerebro
    status: str = "Stopped"  # Stati: Stopped, Running, Error

    API_KEY = os.environ['ALPACA_API_KEY']
    SECRET_KEY = os.environ['ALPACA_SECRET_KEY']

    def __post_init__(self):
        logger.info(f"Inizializzazione dell'istanza {self.name}...")
        self.set_broker()
        self.load_data()
        self.load_strategy()

        # Aggiungi altre operazioni necessarie


    def load_strategy(self):
        #Per testing
        #self.cerebro.addstrategy(DummyStrategy)
        self.cerebro.addstrategy(eval(f'strategies.{self.strategy_name}'))


    def load_data(self):
        logger.info(f"Caricamento dati da {self.feed_list}")
        stock_data = PyTickerSymbols()
        # Ottieni le informazioni sugli indici e i loro simboli
        nasdaq_100_stocks = stock_data.get_stocks_by_index(self.feed_list)
        
        # Estrai solo i simboli e i nomi delle aziende
        for stock in islice(nasdaq_100_stocks, 400):
            logger.info(stock['symbol'])
            data = al_data.AlpacaLiveData(
                symbol=stock['symbol'],
                api_key=self.API_KEY,
                secret_key=self.API_SECRET,
                timeframe=bt.TimeFrame.Minutes,
                compression=1
            )
            self.cerebro.adddata(data)
            symbol = data
        # Logica per caricare i dati

    def set_broker(self):
        logger.info(f"Impostazione del broker {self.broker_name}")
        # Logica per impostare il broker
        broker = None
        if self.broker_name == "default":
            pass
        elif self.broker_name == "alpaca-paper":
                # Configura il broker personalizzato per Alpaca
            broker = al.AlpacaBroker(
                api_key=self.API_KEY,
                secret_key=self.API_SECRET,
                paper=True,
                commission_perc=0.1,  # 0.1% di commissione
                commission_fixed=5.0  # $5 di commissione fissa
            )
            self.cerebro.broker = broker
            logger.info(f"Alpaca paper runnign with cash: {self.cerebro.broker.getcash()} and fund {self.cerebro.broker.getvalue()}")

        elif self.broker_name == "alpaca-live":
                # Configura il broker personalizzato per Alpaca
            broker = al.AlpacaBroker(
                api_key=self.API_KEY,
                secret_key=self.API_SECRET,
                paper=False,
                commission_perc=0.1,  # 0.1% di commissione
                commission_fixed=5.0  # $5 di commissione fissa
            )
            self.cerebro.broker = broker
        else:
            logger.warning(f"Selected {self.broker_name} not found, fall back to default")
            self.broker_name == "default"

    def update_status(self, status):
        self.status=status

 # Aggiungi la strategia (sostituisci con la tua strategia)
class DummyStrategy(mts.MultiTickerStrategy):
    def __init__(self):
        logger.info("---> self.getpositions()")
        pos = self.getpositions()
        for k,p in pos.items():
            logger.info(f"-----------------          {k}:{p.size}:{p.price}")
        p=self.getposition(symbol)
        logger.info(f"-----------------          {p.size}:{p.price}")
        if p.size>0:
            self.close(data=symbol)
    def next(self):
        dt = self.data.datetime.datetime(0)
        logger.info("Nuovo bar ricevuto a %s - Prezzo: %.2f", dt, self.data.close[0])
        p=self.getposition("AZN")
        if p.size>0:
            self.close(data=self.data)
        else:
            self.buy(data=self.data, size=1)



class CerebroManager:
    def __init__(self):
        self.instances = {}  # Dizionario delle istanze: {nome: CerebroInstance}
        self.lock = threading.Lock()  # Per gestire l'accesso concorrente
        # Crea un'istanza di Cerebro
        self.create_instance(name="Alpaca", feed_list="NASDAQ 100", 
                             broker_name="alpaca-paper", strategy_name="weekly.RMAStrategy")
        #self.start_instance(name="Alpaca")

    def create_instance(self, name: str, feed_list: str, broker_name: str, strategy_name: str):
        with self.lock:
            if name in self.instances:
                raise ValueError(f"Instance with name {name} already exists")
            
            # Crea un'istanza di Cerebro
            cerebro = Cerebro()
            
            # Aggiungi feed, broker e strategia (da implementare)
            # Esempio: cerebro.adddata(...), cerebro.addstrategy(...), cerebro.broker = ...
            
            instance = CerebroInstance(name, feed_list, broker_name, strategy_name, cerebro)
            self.instances[name] = instance
            logger.info(f"Created new Cerebro instance: {name}")
            return instance

    def start_instance(self, name: str):
        with self.lock:
            if name not in self.instances:
                raise ValueError(f"Instance {name} not found")
            
            instance = self.instances[name]
            if instance.status == "Running":
                raise ValueError(f"Instance {name} is already running")
            
            try:
                # Avvia l'istanza di Cerebro in un thread separato
                instance.update_status("Running")
                thread = threading.Thread(target=self._run_cerebro, args=(instance,))
                thread.start()
                logger.info(f"Started Cerebro instance: {name}")
            except Exception as e:
                instance.update_status("Error")
                logger.error(f"Failed to start instance {name}: {e}")
                raise

    def _run_cerebro(self, instance: CerebroInstance):
        try:    
            instance.cerebro.run()
        except Exception as e:
            instance.update_status("Error")
            logger.exception(f"Error in Cerebro instance {instance.name}: {e}")
        finally:
            instance.update_status("Stopped")
            logger.info(f"Cerebro instance {instance.name} stopped")

    def stop_instance(self, name: str):
        with self.lock:
            if name not in self.instances:
                raise ValueError(f"Instance {name} not found")
            
            instance = self.instances[name]
            if instance.status != "Running":
                raise ValueError(f"Instance {name} is not running")
            
            try:
                # Ferma l'istanza di Cerebro
                instance.cerebro.runstop()
                instance.update_status("Stopped")
                logger.info(f"Stopped Cerebro instance: {name}")
            except Exception as e:
                instance.update_status("Error")
                logger.exception(f"Failed to stop instance {name}: {e}")
                raise

    def get_instance(self, name: str):
        with self.lock:
            if name not in self.instances:
                raise ValueError(f"Instance {name} not found")
            return self.instances[name]

    def list_instances(self):
        with self.lock:
            return list(self.instances.values())