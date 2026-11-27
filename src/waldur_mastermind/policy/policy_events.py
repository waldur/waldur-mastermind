"""Events that log a policy change made by a person.

Policies are saved by the background tasks too (has_fired, fired_datetime),
so the events are emitted by whoever saves on a person's behalf: the policy
endpoints, and any other path that saves a policy through its serializer.
"""

from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType


def emit_policy_event(user, policy, event_type, verb, changes=""):
    scope = policy.scope
    message = (
        "{policy_type} {policy_uuid} for {policy_scope_type} "
        "{policy_scope_name} has been %s by {user_username}." % verb
    )
    if changes:
        message += " Changes: {policy_changes}."
    event_logger.emit(
        message,
        event_type=event_type,
        event_context={
            "user": user,
            "policy_type": policy.__class__.__name__,
            "policy_uuid": policy.uuid.hex,
            "policy_scope_type": scope.__class__.__name__,
            "policy_scope_uuid": scope.uuid.hex,
            "policy_scope_name": scope.name,
            "policy_actions": policy.actions,
            "policy_changes": changes,
        },
        scopes=[scope],
    )


def describe_related_item(item):
    # Nested limits are deleted and recreated on every update and have no
    # __str__, so they are compared by their field values, not identity.
    parts = []
    for field in item._meta.concrete_fields:
        if field.primary_key or field.name in ("policy", "created", "modified"):
            continue
        value = getattr(item, field.name)
        # Names repeat across offerings (every one has a "CPU"), so a
        # related object is told apart by its UUID as well.
        if field.is_relation and hasattr(value, "uuid"):
            value = f"{value} [{value.uuid.hex}]"
        parts.append(f"{field.name}={value}")
    return "(" + ", ".join(parts) + ")"


def get_field_values(policy, fields):
    values = {}
    for field in fields:
        value = getattr(policy, field, None)
        if hasattr(value, "all"):
            value = sorted(describe_related_item(item) for item in value.all())
        values[field] = value
    return values


def save_updated(user, serializer, save):
    """Update a policy with ``save()`` and log what changed, if anything.

    Clients send the whole form, so the submitted fields are not the changed
    ones; the values are compared instead.
    """
    fields = sorted(serializer.validated_data)
    old_values = get_field_values(serializer.instance, fields)
    save()
    new_values = get_field_values(serializer.instance, fields)
    changes = ", ".join(
        f"{field}: {old_values[field]} -> {new_values[field]}"
        for field in fields
        if old_values[field] != new_values[field]
    )
    if changes:
        emit_policy_event(
            user, serializer.instance, EventType.POLICY_UPDATED, "updated", changes
        )


def save_with_events(user, serializer):
    """Save a policy serializer as the policy endpoints do, events included."""
    if serializer.instance is None:
        policy = serializer.save()
        emit_policy_event(user, policy, EventType.POLICY_CREATED, "created")
    else:
        save_updated(user, serializer, serializer.save)
