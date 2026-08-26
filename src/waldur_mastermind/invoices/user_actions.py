import datetime
from typing import Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from waldur_core.structure.managers import (
    filter_queryset_for_user,
    get_connected_customers,
)
from waldur_core.user_actions.providers import (
    BaseDashboardProvider,
    register_dashboard_provider,
)

from . import models

User = get_user_model()


class DashboardOverdueInvoiceProvider(BaseDashboardProvider):
    """Provider for invoices past their due date."""

    action_type = "invoice_overdue"
    display_name = "Overdue Invoices"

    def get_dashboard_pending_actions(self, user: User) -> list[dict[str, Any]]:
        # Same scope as /api/invoices/, which the feed item links to. Invoice
        # visibility is customer-scoped only, so project members are not nagged
        # about payment they cannot see. The extra customer narrowing matters
        # for staff, whom filter_queryset_for_user hands the whole platform —
        # this feed is a personal dashboard, not an admin report.
        visible = filter_queryset_for_user(models.Invoice.objects.all(), user).filter(
            customer__in=get_connected_customers(user)
        )
        # due_date is invoice_date + PAYMENT_INTERVAL, so the whole predicate
        # pushes into SQL instead of loading every issued invoice to discard
        # most of them.
        payment_interval = settings.WALDUR_INVOICES["PAYMENT_INTERVAL"]
        cutoff = timezone.now().date() - datetime.timedelta(days=payment_interval)
        overdue = (
            visible.filter(
                state=models.Invoice.States.CREATED,
                invoice_date__lt=cutoff,
            )
            .select_related("customer")
            .order_by("invoice_date")
        )
        # Aggregated rather than one item per invoice, like the other three
        # providers. This feed is rebuilt on every dashboard load, and an owner
        # of several organisations with a backlog of unpaid invoices otherwise
        # pushed dozens of rows into it. The deep-link uuids still ride along
        # in the single-invoice case, which is the one worth linking.
        oldest = overdue.first()
        if oldest is None:
            return []
        count = overdue.count()
        single = count == 1
        single_customer = (
            overdue.values_list("customer_id", flat=True).distinct().count() == 1
        )
        if single:
            description = _(
                "Payment for invoice %(number)d (%(customer)s) is overdue."
            ) % {"number": oldest.number, "customer": oldest.customer.name}
        else:
            description = _("You have %(count)d invoices past their due date.") % {
                "count": count
            }
        return [
            {
                "type": "invoice_overdue",
                "title": ngettext(
                    "%(count)d invoice overdue",
                    "%(count)d invoices overdue",
                    count,
                )
                % {"count": count},
                "description": description,
                "variant": "error",
                # The oldest invoice's due date: the deadline shown is the one
                # that passed first.
                "deadline": timezone.make_aware(
                    datetime.datetime.combine(
                        oldest.due_date, datetime.datetime.min.time()
                    )
                ),
                "count": count,
                # Only a single overdue invoice has a detail page worth
                # deep-linking to. The customer still rides along whenever every
                # overdue invoice belongs to the same one, so the aggregate can
                # link to that organization's invoice list instead of being a
                # dead row.
                "target_uuid": oldest.uuid if single else None,
                "customer_uuid": (
                    oldest.customer.uuid if single or single_customer else None
                ),
            }
        ]


register_dashboard_provider(DashboardOverdueInvoiceProvider)
