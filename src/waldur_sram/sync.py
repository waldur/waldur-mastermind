"""Bring a SRAM group's placeholder role and project-rule grants up to date."""

from . import models, roles, rules


def sync_group(group: models.SramGroup) -> None:
    with rules.deferred():
        roles.sync_members(group)
    rules.reconcile_groups(rules.related_groups(group))


def delete_group(group: models.SramGroup) -> None:
    """Revoke everything the group granted, delete it, then re-check its groups.

    A collaboration's groups take their labels and placeholders from it, so
    without it their rule grants no longer hold.
    """
    dependants = [g for g in rules.related_groups(group) if g.pk != group.pk]
    customer_id = group.customer_id
    with rules.deferred():
        touched = rules.revoke_group(group)
        roles.delete_role(group)
        group.delete()
    if customer_id:
        touched.add(customer_id)
    rules.reconcile_groups(dependants)
    rules.reconcile_groups(
        models.SramGroup.objects.filter(customer_id__in=touched).select_related(
            "customer", "role"
        )
    )
