"""The URL an order calls back when its state changes.

A Waldur that creates orders on another Waldur (marketplace_remote) sends the
order's ``callback_url``: its own public pull endpoint for that order. Order
create accepts the field and does not store it, so callbacks are off; an
order whose URL was stored earlier, or set by ``import_structure``, is still
called by ``marketplace_remote.tasks.trigger_order_callback`` on every state
change.

Only that endpoint is accepted. On write only the URL's shape and path are
checked, with no DNS lookup; before each call the host's addresses must also
pass the outbound address check, and the connection is pinned to them.
"""

import re
from urllib.parse import urlsplit

from django.utils.translation import gettext_lazy as _

from waldur_core.core.client import ClientValidationError, resolve_and_check, split_url

# The pull endpoint in marketplace_remote/urls.py ("pull_remote_order"), as
# processors.build_callback_url appends it to MASTERMIND_URL, which may carry
# a path prefix. The prefix is plain, non-empty segments: no percent-encoding,
# and no "." or ".." that a server would resolve into another path. The one
# doubled slash allowed is the one a MASTERMIND_URL with a trailing slash
# leaves before "/api".
PULL_ORDER_PATH = re.compile(
    r"(?:/(?!\.\.?/)[A-Za-z0-9._~-]+)*/?/api/remote-waldur-api/pull_order/[0-9a-f]{32}"
)


class CallbackUrlRefused(Exception):
    """The message is safe to show to whoever set the URL."""


def check_callback_url_shape(url) -> tuple[str, int]:
    """Return the host and port of ``url``, or raise ``CallbackUrlRefused``.

    Looks only at the URL itself: no DNS lookup, so a caller cannot make the
    web worker wait on a resolver of their choosing.
    """
    try:
        hostname, port = split_url(url)
    except ClientValidationError as exc:
        raise CallbackUrlRefused(str(exc)) from None
    if not PULL_ORDER_PATH.fullmatch(urlsplit(url).path):
        raise CallbackUrlRefused(
            _("Callback URL must be the order pull endpoint of a Waldur.")
        )
    return hostname, port


def check_callback_url(url) -> tuple[str, list[str]]:
    """Return the host of ``url`` and its checked addresses, or raise
    ``CallbackUrlRefused``. Resolves the host: only for the worker."""
    hostname, port = check_callback_url_shape(url)
    try:
        addresses = resolve_and_check(hostname, port)
    except ClientValidationError:
        # One answer for internal and unresolvable hosts alike, so that it
        # does not tell which internal names exist.
        raise CallbackUrlRefused(
            _("Callback URL host is not allowed or cannot be resolved.")
        ) from None
    return hostname, addresses
