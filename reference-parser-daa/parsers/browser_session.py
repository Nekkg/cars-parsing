from selenium.common.exceptions import InvalidSessionIdException, NoSuchWindowException, WebDriverException


def session_expired(error):
    return isinstance(error, (InvalidSessionIdException, NoSuchWindowException)) or (
        isinstance(error, WebDriverException) and any(s in str(error).lower() for s in (
            'unable to find session', 'invalid session id', 'session timed out', 'session deleted', 'disconnected:')))


class RecoveringBrowser:
    """Retry the interrupted browser operation once with a fresh session."""
    def __init__(self, factory, on_recovery=None):
        self.factory = factory
        self.on_recovery = on_recovery or (lambda: None)
        self.driver = factory()
        self.last_url = None

    def _call(self, name, args=None, kwargs=None):
        if name == 'get' and args: self.last_url = args[0]
        for attempt in range(2):
            try:
                value = getattr(self.driver,name)
                return value(*args,**kwargs) if args is not None else value
            except Exception as error:
                if attempt or not session_expired(error): raise
                self.on_recovery()
                try: self.driver.quit()
                except Exception: pass
                self.driver = self.factory()
                if name != 'get' and self.last_url:
                    self.driver.get(self.last_url)

    def __getattr__(self,name):
        value = self._call(name)
        if callable(value):
            return lambda *args,**kwargs: self._call(name,args,kwargs)
        return value

    def quit(self):
        return self.driver.quit()
