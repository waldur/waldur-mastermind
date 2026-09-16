from django.db import transaction

from waldur_core.permissions.models import UserRole
from waldur_core.structure.models import Customer, Project

from . import models, roles, rules


def rename_placeholder_roles_on_slug_change(
    sender, instance: Customer, created=False, **kwargs
):
    """Placeholder role names embed the organization slug; keep them in step."""
    if created:
        return
    old_slug = getattr(instance, "_old_slug", None)
    if not old_slug or old_slug == instance.slug:
        return
    roles.rename_roles_for_customer(instance)


def reconcile_rules_on_placeholder_change(sender, instance: UserRole, **kwargs):
    """A hand-made placeholder grant or revocation changes who rules apply to."""
    if rules.is_deferred() or not rules.enabled():
        return
    group = (
        models.SramGroup.objects.filter(role_id=instance.role_id)
        .select_related("customer", "role")
        .first()
    )
    if group is not None:
        rules.reconcile_group(group)


def reconcile_rules_on_project_change(
    sender, instance: Project, created=False, **kwargs
):
    """Rules select projects by backend_id or slug, so a new or changed project
    may gain or lose rule grants."""
    if not rules.enabled():
        return
    fields = ("backend_id", "slug", "customer_id")
    if not created and not any(instance.tracker.has_changed(f) for f in fields):
        return
    customer_ids = {instance.customer_id}
    if not created and instance.tracker.has_changed("customer_id"):
        customer_ids.add(instance.tracker.previous("customer_id"))
    for customer_id in customer_ids:
        if customer_id:
            rules.reconcile_groups(rules.groups_for_customer(customer_id))


def reconcile_rule_on_save(sender, instance: models.SramProjectRule, **kwargs):
    """Applies to existing holders too; runs for the API and the admin alike."""
    if not rules.enabled():
        return
    transaction.on_commit(lambda: rules.reconcile_rule(instance))


def revoke_rule_on_delete(sender, instance: models.SramProjectRule, **kwargs):
    if not rules.enabled():
        return
    uuid_hex, name = instance.uuid.hex, instance.name
    transaction.on_commit(lambda: rules.revoke_rule(uuid_hex, name))
