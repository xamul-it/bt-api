
class EventEmitter:
    _instance = None
    EV_TICKER_CHANGED = "EV_TICKER_CHANGED"
    #Evento generato quando vengono scaricati dati nuovi
    EV_TICKER_FETCHED = "EV_TICKER_FETCHED"
    #Evento generato quando le liste dei ticker cambiano
    EV_TICKERLIST_CHANGE ="EV_TICKERLIST_CHANGE"
    #Generico ???
    EV_RUN_BACKTRADER = "RUN_BACKTRADER"

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(EventEmitter, cls).__new__(cls)
            # Inizializza qui qualsiasi variabile di istanza
            cls._instance.listeners = {}
        return cls._instance

    def __init__(self):
        pass

    def on(self, event, callback):
        self.listeners.setdefault(event, []).append(callback)

    def emit(self, event, *args, **kwargs):
        for callback in self.listeners.get(event, []):
            callback(*args, **kwargs)


