"""Reuse catalog reads inside one customer reply only, never across updates.

Owner edits and all financial/security state bypass this cache. Workers inherit
the same reply-local context, but callers receive copies to avoid shared edits.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
from threading import RLock

_scope = ContextVar('catalog_read_scope', default=None)


@contextmanager
def customer_catalog_reads(enabled=True):
    token = _scope.set(({}, RLock()) if enabled else None)
    try:
        yield
    finally:
        _scope.reset(token)


def catalog_read(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        scope = _scope.get()
        if scope is None:
            return function(*args, **kwargs)
        cache, lock = scope
        key = (function, args, tuple(sorted(kwargs.items())))
        with lock:
            if key not in cache:
                cache[key] = deepcopy(function(*args, **kwargs))
            return deepcopy(cache[key])
    return wrapped
