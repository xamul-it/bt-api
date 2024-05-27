class EventEmitter:
    _instance = None

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