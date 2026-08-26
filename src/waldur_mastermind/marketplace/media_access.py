"""Media access rules for files owned by the marketplace app.

See :mod:`waldur_core.media.access`.
"""

from waldur_core.media import access
from waldur_mastermind.marketplace.managers import filter_orders_for_user
from waldur_mastermind.marketplace.models import (
    Category,
    CategoryGroup,
    Offering,
    OfferingFile,
    OfferingGroup,
    Order,
    Screenshot,
    ServiceProvider,
)

# Everything below is reachable anonymously today through PublicOfferingViewSet
# (permission_classes = []) or a PublicViewsetMixin listing, which is what the
# unauthenticated marketplace catalogue renders from.
#
# The offering, screenshot and thumbnail prefixes must stay unconditionally
# public, and in particular must not be gated on ANONYMOUS_USER_CAN_VIEW_OFFERINGS:
# marketplace_remote.utils pulls those bytes over a bare unauthenticated
# httpx.get of the media URL when importing a remote offering, and swallows
# failures into a log line.
access.register_public(access.upload_prefix(Category, "icon"))
access.register_public(access.upload_prefix(CategoryGroup, "icon"))
access.register_public(access.upload_prefix(Offering, "thumbnail"))
access.register_public(access.image_prefix(Offering))
access.register_public(access.image_prefix(ServiceProvider))
# Screenshot.image and Screenshot.thumbnail both use get_upload_path, so one
# prefix covers both.
access.register_public(access.image_prefix(Screenshot))
# Attached documentation, serialized into the public offering payload.
access.register_public(access.upload_prefix(OfferingFile, "file"))

# Offering group icons are not on any anonymous serializer.
access.register_authenticated(access.upload_prefix(OfferingGroup, "icon"))

# Order attachments carry purchase orders and the provider/consumer message
# threads -- terms of service, agreements, PI details. Gate them on the same
# predicate OrderViewSet lists orders with.
# Each field has its own upload_to, so the prefix identifies the column and the
# lookup stays single-column.
ORDER_ATTACHMENT_FIELDS = (
    "attachment",
    "provider_message_attachment",
    "consumer_message_attachment",
)

for field_name in ORDER_ATTACHMENT_FIELDS:
    access.register(
        access.upload_prefix(Order, field_name),
        access.queryset_rule(Order, [field_name], filter_orders_for_user),
    )
