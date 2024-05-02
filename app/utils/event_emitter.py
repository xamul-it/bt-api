class EventEmitter:
    def __init__(self):
        self.listeners = {}

    def on(self, event, callback):
        self.listeners.setdefault(event, []).append(callback)

    def emit(self, event, *args, **kwargs):
        for callback in self.listeners.get(event, []):
            callback(*args, **kwargs)


