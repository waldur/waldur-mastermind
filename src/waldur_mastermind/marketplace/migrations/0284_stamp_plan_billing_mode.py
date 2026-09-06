"""Give OpenStack tenant plans an explicit billing mode.

Until now a plan left on ``inherit`` followed whatever billing type its
offering's builtin components happened to carry, so switching the components
silently changed how existing plans bill. Stamping the mode that is in effect
today freezes each plan's current behaviour; the components can then be
retired as the place where billing is chosen.

**Only OpenStack tenant offerings are stamped.** The per-plan billing mode is
a general mechanism, but it is inert until a plan holds a mode other than
``inherit``: the resolver returns the component's own values, so every path
added for it behaves exactly as before. OpenStack is where the feature is
wanted and where it has been exercised, so it is the only offering type moved
off ``inherit`` here. Every other deployment -- VMware, Rancher, OpenPortal,
SLURM, site agent -- keeps the behaviour it has today, byte for byte, and a
provider there can still adopt a mode deliberately through the API.

Within OpenStack a plan still keeps ``inherit`` when no single mode reproduces
what it bills: when the offering has no builtin components, when those
components disagree with each other, and -- above all -- whenever resolving
them under the candidate mode would not give back what they say today.
Nothing here may change a price.
"""

from django.db import migrations

from waldur_mastermind.marketplace.enums import (
    OPENSTACK_TENANT_OFFERING,
    BillingModes,
    BillingTypes,
)


class _PlanStub:
    """Just enough of a plan for the resolver, which only reads the mode."""

    def __init__(self, billing_mode):
        self.billing_mode = billing_mode


def _stamp_preserves_billing(components, mode):
    """Whether resolving under ``mode`` gives what the components say today.

    An explicit mode recomputes the limit period and the measured unit rather
    than reading them, so it does not reproduce a component the provider has
    tuned: a limit period of annual or total becomes month, which would bill a
    subscription every month that is billed once a year or once outright, and a
    renamed unit reverts to the canonical one. Those offerings keep inherit,
    which goes on reading the components exactly as before.

    The comparison runs through the production resolver so it cannot drift
    from what the application will actually do with the stamped mode.
    """
    from waldur_mastermind.marketplace import billing_mode

    stored = _PlanStub(BillingModes.INHERIT)
    stamped = _PlanStub(mode)
    return all(
        billing_mode.resolve_component(component, stored)
        == billing_mode.resolve_component(component, stamped)
        for component in components
    )


def _mode_of(component):
    """The plan billing mode that reproduces this component's billing."""
    if component.billing_type == BillingTypes.USAGE:
        return BillingModes.USAGE
    if component.billing_type == BillingTypes.LIMIT and not component.is_prepaid:
        return BillingModes.LIMIT
    # Prepaid, fixed, plan-switch and plain one-time components have no plan
    # mode -- prepaid deliberately so, it stays on the component -- and a plan
    # over them has to keep reading the component.
    return None


def stamp_billing_mode(apps, schema_editor):
    Offering = apps.get_model("marketplace", "Offering")

    offerings = Offering.objects.filter(
        type=OPENSTACK_TENANT_OFFERING
    ).prefetch_related("components", "plans")
    for offering in offerings.iterator(chunk_size=500):
        # 0283 set this for exactly the components the plugin provides.
        builtins = [
            component
            for component in offering.components.all()
            if component.billed_per_plan
        ]
        if not builtins:
            continue

        modes = {_mode_of(component) for component in builtins}
        if len(modes) != 1:
            # The components disagree, so no single mode reproduces them.
            continue
        mode = modes.pop()
        if mode is None:
            continue
        if not _stamp_preserves_billing(builtins, mode):
            continue

        offering.plans.filter(billing_mode=BillingModes.INHERIT).update(
            billing_mode=mode
        )


def unstamp(apps, schema_editor):
    """Nothing to undo.

    Both modes this migration writes were reachable before it and may have been
    set deliberately, and either way they reproduce what the components say, so
    resetting them would be guesswork. Reversing is a no-op, which also keeps a
    re-apply idempotent.
    """


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0283_offeringcomponent_billed_per_plan"),
    ]

    operations = [
        migrations.RunPython(stamp_billing_mode, unstamp),
    ]
