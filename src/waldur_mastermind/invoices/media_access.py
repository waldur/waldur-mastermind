"""Media access rules for files owned by the invoices app.

See :mod:`waldur_core.media.access`.
"""

from waldur_core.media import access
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_mastermind.invoices.models import Payment

# Payment.Permissions routes through profile__organization, which is what
# PaymentViewSet's GenericRoleFilter applies.
access.register(
    access.upload_prefix(Payment, "proof"),
    access.queryset_rule(Payment, ["proof"], filter_queryset_for_user),
)
