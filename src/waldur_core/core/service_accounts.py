"""Making sign-ins to service accounts visible.

A service account (``User.is_service_account``) is created for an integration
and normally authenticates with its token. Single sign-on still finds it by
username like any other account, and is allowed to: a team may log in to it on
purpose. As that is also what a username collision looks like, every such
sign-in is logged at WARNING and recorded as an event.
"""

import logging

from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType

logger = logging.getLogger(__name__)


def note_service_account_sign_in(user, via: str, identity: str = "") -> None:
    """Record that ``user``, a service account, was signed in to ``via`` an
    identity provider, as ``identity`` (subject, issuer)."""
    if not getattr(user, "is_service_account", False):
        return
    logger.warning(
        "Service account %r was signed in to via %s (identity %s). If no one "
        "was meant to log in to it, treat this as a takeover by username.",
        user.username,
        via,
        identity or "unknown",
    )
    event_logger.emit(
        "Service account {affected_user_username} was signed in to via "
        "{sign_in_method}.",
        event_type=EventType.SERVICE_ACCOUNT_SIGNED_IN,
        event_context={
            "affected_user": user,
            "sign_in_method": via,
            "identity": identity,
        },
        scopes=[user],
    )
