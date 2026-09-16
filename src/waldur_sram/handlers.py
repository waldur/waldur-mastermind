from waldur_core.structure.models import Customer

from . import roles


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
