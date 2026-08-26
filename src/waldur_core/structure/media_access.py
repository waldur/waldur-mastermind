"""Media access rules for files owned by the structure app.

See :mod:`waldur_core.media.access`.
"""

from waldur_core.media import access
from waldur_core.structure.models import (
    Customer,
    ExternalLink,
    Project,
    ServiceSettings,
)

# Customer logos ride on ServiceProviderSerializer.customer_image, served
# anonymously by ServiceProviderViewSet (PublicViewsetMixin).
access.register_public(access.image_prefix(Customer))

# ExternalLinkViewSet uses IsAdminOrReadOnly, which allows SAFE_METHODS without
# authenticating, so these images are anonymous today.
access.register_public(access.image_prefix(ExternalLink))

# Project logos are rendered on the public group-invitation landing page:
# GroupInvitationViewSet allows anonymous list/retrieve, and
# GroupInvitationSerializer.get_scope_image emits scope.image.url for a scope
# that may be either a Customer or a Project.
access.register_public(access.image_prefix(Project))

# ServiceSettings.certificate sits alongside the encrypted password and token
# fields. The ServiceSettings read rule makes every shared=True row readable by
# any authenticated user; a credential-adjacent artifact must not inherit that,
# so this is deliberately narrower than the ViewSet.
access.register_staff_only(access.upload_prefix(ServiceSettings, "certificate"))
