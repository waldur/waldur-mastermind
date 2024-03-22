from waldur_core.permissions.log import event_logger
from waldur_core.structure.permissions import _get_customer


def log(instance, current_user, message, event_type):
    model_name = instance.scope._meta.model_name
    role_name = instance.role.name
    customer = _get_customer(instance.scope)
    event_context = {
        "scope": instance.scope,
        "scope_type": model_name,
        "scope_uuid": instance.scope.uuid.hex,
        "scope_name": instance.scope.name,
        "customer": customer,
        "affected_user": instance.user,
        "role_name": role_name,
    }
    if current_user:
        event_context["user"] = current_user
    event_logger.user_role.info(
        message, event_type=event_type, event_context=event_context
    )


def log_role_granted(sender, instance, current_user=None, **kwargs):
    affected_user = instance.user.full_name or instance.user.username
    role_name = instance.role.name
    log(
        instance,
        current_user,
        message=f"User {affected_user} has gained role of {role_name} in {instance.scope.name}.",
        event_type="role_granted",
    )


def log_role_revoked(sender, instance, current_user=None, **kwargs):
    affected_user = instance.user.full_name or instance.user.username
    role_name = instance.role.name
    log(
        instance,
        current_user,
        message=f"User {affected_user} has lost role of {role_name} in {instance.scope.name}.",
        event_type="role_revoked",
    )


def log_role_updated(sender, instance, current_user=None, **kwargs):
    affected_user = instance.user.full_name or instance.user.username
    old_time = instance.tracker.previous("expiration_time")
    new_time = instance.expiration_time
    log(
        instance,
        current_user,
        message=f"Permission expiration time for user {affected_user} "
        f"in {instance.scope.name} is updated from {old_time} to {new_time}.",
        event_type="role_updated",
    )
