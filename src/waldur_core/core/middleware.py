import threading

_locals = threading.local()


def set_current_user(user):
    _locals.user = user


def get_current_user():
    return getattr(_locals, "user", None)
