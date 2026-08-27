"""Give one-time plan components the amount the invoice has been charging.

``PlanComponent.amount`` defaults to 0, and the invoice ignored it for
ONE_TIME and ON_PLAN_SWITCH components — it charged one of each whatever the
plan said. So a plan that never set an amount was quoted nothing for a fee its
customer was billed in full, and nothing distinguished "deliberately none"
from "never filled in".

The invoice now reads the amount, which makes the two sides agree but would
silently stop charging every fee sitting at the default. Writing 1 into those
rows keeps the charge exactly as it is today and makes the estimate finally
say so. A provider who wants no fee can now set 0 and have both sides honour
it, which was not previously expressible.

Components already carrying an amount are left alone: those were being
under-charged against their own quote, and correcting that is the point.
"""

from django.db import migrations

BILLING_TYPES = ("one", "few")


def set_default_amount(apps, schema_editor):
    PlanComponent = apps.get_model("marketplace", "PlanComponent")
    PlanComponent.objects.filter(
        component__billing_type__in=BILLING_TYPES,
        component__is_prepaid=False,
        amount=0,
    ).update(amount=1)


def unset_default_amount(apps, schema_editor):
    # Not reversible in any meaningful sense: a 1 written here is
    # indistinguishable afterwards from a 1 a provider chose. Left as a no-op
    # rather than clearing amounts the operator may since have relied on.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0277_order_mp_order_attachment_idx_and_more"),
    ]

    operations = [
        migrations.RunPython(set_default_amount, unset_default_amount),
    ]
