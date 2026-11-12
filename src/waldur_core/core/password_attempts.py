"""A limit on failed password attempts per client address.

Accounts are locked by django-axes, per username. That does nothing against
one address trying a few passwords on each of many accounts, so failed
attempts are also counted per address, across usernames. When an address
reaches ``PASSWORD_FAILURES_PER_ADDRESS_LIMIT`` failures within
``PASSWORD_FAILURES_PER_ADDRESS_WINDOW_MINUTES`` of its first one, it is
refused for ``PASSWORD_FAILURES_PER_ADDRESS_COOLOFF_MINUTES``, and counting
then starts afresh. Only failures count: many users share an address, and
their correct sign-ins must never add up to a refusal.

The address is the one axes records (``AXES_CLIENT_IP_CALLABLE``), always a
parsed address. IPv6 addresses count per /64, which one client usually has
all of.

The counter is a set of numbered slots per address and window, each taken
with ``cache.add``. An increment of one cache value is a read and a write on
the database cache, so concurrent failures would be lost; a slot is taken by
exactly one of them.
"""

import ipaddress
import math
import time

from axes.helpers import get_client_ip_address
from django.core.cache import cache

from waldur_core.configuration import config

KEY_PREFIX = "PASSWORD_FAILURES"
IPV6_PREFIX_LENGTH = 64


def _counted_address(request) -> str | None:
    """What failures are counted against: the address, or its /64."""
    try:
        address = ipaddress.ip_address(get_client_ip_address(request) or "")
    except ValueError:
        return None
    if address.version == 6:
        return str(
            ipaddress.ip_network(f"{address}/{IPV6_PREFIX_LENGTH}", strict=False)
        )
    return str(address)


def _blocked_key(address) -> str:
    return f"{KEY_PREFIX}_BLOCKED_{address}"


def _window_key(address) -> str:
    return f"{KEY_PREFIX}_WINDOW_{address}"


def _minutes(value) -> int:
    return max(1, value) * 60


def retry_after(request) -> int | None:
    """Seconds until this request's address may try a password again, or None
    when it may now."""
    if config.PASSWORD_FAILURES_PER_ADDRESS_LIMIT <= 0:
        return None
    address = _counted_address(request)
    if address is None:
        return None
    until = cache.get(_blocked_key(address))
    if until is None:
        return None
    left = until - time.time()
    return math.ceil(left) if left > 0 else None


def _current_window(address, period) -> str:
    """The id of the address's counting window, opened by its first failure."""
    key = _window_key(address)
    opened = f"{time.time():.6f}"
    if cache.add(key, opened, period):
        return opened
    # It may expire between the two calls; this failure then opens the next.
    return cache.get(key) or opened


def record_failure(request):
    """Count a failed password attempt against the request's address, and
    refuse the address for the cool-off once it reaches the limit."""
    limit = config.PASSWORD_FAILURES_PER_ADDRESS_LIMIT
    if limit <= 0:
        return
    address = _counted_address(request)
    if address is None:
        return
    period = _minutes(config.PASSWORD_FAILURES_PER_ADDRESS_WINDOW_MINUTES)
    window = _current_window(address, period)
    counter_key = f"{KEY_PREFIX}_{address}_{window}"
    # Where the next free slot probably is. A concurrent failure may have taken
    # it, and then the next one up is tried.
    n = (cache.get(counter_key) or 0) + 1
    while n <= limit and not cache.add(f"{counter_key}_{n}", True, period):
        n += 1
    cache.set(counter_key, min(n, limit), period)
    # At or past it: a limit lowered while an address is above the new value
    # applies at its next failure.
    if n >= limit:
        cooloff = _minutes(config.PASSWORD_FAILURES_PER_ADDRESS_COOLOFF_MINUTES)
        cache.set(_blocked_key(address), time.time() + cooloff, cooloff)
        # After the cool-off the address starts with a clean count.
        cache.delete(_window_key(address))
