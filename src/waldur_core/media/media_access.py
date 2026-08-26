"""Media access rules for files owned by the media app itself.

See :mod:`waldur_core.media.access`.
"""

from waldur_core.media import access
from waldur_core.media.utils import MARKDOWN_IMAGE_PREFIX

# Markdown images are embedded in offering descriptions, which are rendered on
# the anonymous public offering page.
access.register_public(MARKDOWN_IMAGE_PREFIX)
