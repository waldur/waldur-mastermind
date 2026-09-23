"""Media access rules for files owned by the core app.

See :mod:`waldur_core.media.access`.
"""

from waldur_core.core.models import User
from waldur_core.media import access

# Avatars are rendered across the UI with <img src> and CSS background-image,
# neither of which can carry the API token that homeport sends as a header.
# Requiring authentication made every avatar 404 unless the browser happened
# to hold a Django session cookie, which token and OIDC logins never set. The
# URL is keyed by the file's random uuid, as for customer and project logos.
access.register_public(access.image_prefix(User))
