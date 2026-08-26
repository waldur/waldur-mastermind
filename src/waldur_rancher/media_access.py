"""Media access rules for files owned by the rancher plugin.

See :mod:`waldur_core.media.access`.
"""

from waldur_core.media import access
from waldur_rancher.models import Template

access.register_authenticated(access.upload_prefix(Template, "icon"))
