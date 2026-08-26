"""Media access rules for files owned by the support app.

See :mod:`waldur_core.media.access`.
"""

from waldur_core.media import access
from waldur_mastermind.support.models import Attachment, TemplateAttachment

# Issue attachments: reuse the manager the AttachmentViewSet already filters with.
access.register(
    access.upload_prefix(Attachment, "file"),
    access.queryset_rule(
        Attachment,
        ["file"],
        lambda queryset, user: queryset.filter_for_user(user),
    ),
)

# Support templates are a global catalogue; TemplateViewSet is IsAdminOrReadOnly,
# so any logged-in user can already read them through the API.
access.register_authenticated(access.upload_prefix(TemplateAttachment, "file"))
