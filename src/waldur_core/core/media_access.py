"""Media access rules for files owned by the core app.

See :mod:`waldur_core.media.access`.
"""

from waldur_core.core.models import User
from waldur_core.media import access

# Avatars are rendered in member and team listings across the UI. A per-scope
# check would mean a query per avatar, so require a session and stop there.
access.register_authenticated(access.image_prefix(User))
