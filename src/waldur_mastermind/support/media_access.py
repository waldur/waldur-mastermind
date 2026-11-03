"""Media access rules for files owned by the support app.

See :mod:`waldur_core.media.access`.
"""

from waldur_core.media import access
from waldur_mastermind.support.filters import filter_attachments_for_request
from waldur_mastermind.support.models import Attachment, TemplateAttachment

# Issue attachments go to whoever sees their issue, by the filter
# AttachmentViewSet narrows with.
access.register(
    access.upload_prefix(Attachment, "file"),
    access.queryset_rule(Attachment, ["file"], filter_attachments_for_request),
    # A personal access token's staff and support scopes are on the request,
    # and the issue rule narrows by the client's address where an organization
    # limits sign-in to its subnets.
    with_request=True,
)

# Support templates are a global catalogue; TemplateViewSet is IsAdminOrReadOnly,
# so any logged-in user can already read them through the API.
access.register_authenticated(access.upload_prefix(TemplateAttachment, "file"))
