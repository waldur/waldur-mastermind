"""Core, marketplace-free dispatch of user-centric events to GLOBAL consumers.

A global EventConsumer (no ``EventConsumerScope`` bindings at all) is a
staff/support-only queue that receives cross-cutting, user-centric events for
IdM/IGA sync: profile changes, SSH-key changes, user lifecycle, and role
changes on any scope. These events originate in ``waldur_core`` (User, SshKey,
permission roles), so their dispatcher lives here and imports NO marketplace
code — the offering-scoped path stays in ``marketplace.utils.prepare_messages``.

Security boundary: global consumers carry PII, and the boundary is the
staff/support guard at registration (see EventConsumerViewSet), not any
client-side filtering.

A global (empty-scope) consumer means "everything": it receives the
user-centric events dispatched here AND, via the marketplace dispatcher, the
marketplace events (orders, resources, offering users, …). ``include_global``
lets each caller opt globals in. The one care point is USER_ROLE, which BOTH
dispatchers can emit — the core path on the role_granted/revoked signal, the
marketplace path on a manual project re-sync. To avoid delivering it to a global
consumer twice, the core path owns USER_ROLE for globals and the marketplace
path passes ``include_global=False`` for USER_ROLE only. Every other object
type is emitted by exactly one path, so a global receives it once. Suppression
of a user's legacy subscription is keyed on messages actually delivered (see
``DispatchResult``), so a global that genuinely receives a marketplace event
correctly supersedes its own legacy queue for that event.

The payload is produced by a builder callable that runs ONLY when at least one
global consumer exists — so a system with no global consumer pays nothing on
every user save / role change, and payload construction never runs against
half-built instances (e.g. during imports, where a scope uuid may still be a
plain string).
"""

import json
import logging
from typing import NamedTuple

from django.contrib.contenttypes.models import ContentType
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Q

from waldur_core.core.middleware import get_skip_side_effects
from waldur_core.core.user_attributes import get_enabled_profile_attributes
from waldur_core.logging import models as logging_models
from waldur_core.logging import tasks as logging_tasks
from waldur_core.logging.enums import ObservableObjectType
from waldur_core.permissions import utils as permission_utils

logger = logging.getLogger(__name__)

# Model fields that live on User but are never part of the profile signal:
# is_active is owned by the lifecycle event; the rest are auth/security state,
# not profile PII, and must never leave the platform via this channel even if
# they happen to appear in a future enabled-attributes list.
_NON_PROFILE_FIELDS = frozenset(
    {
        "is_active",
        "is_staff",
        "is_support",
        "is_superuser",
        "password",
        "token_lifetime",
    }
)


def _hex(value):
    """UUID hex, tolerant of an already-stringified uuid (import paths)."""
    if value is None:
        return None
    return value.hex if hasattr(value, "hex") else str(value)


def _global_consumers():
    """Consumers with NO bindings (= unrestricted), still owned by a privileged
    user. The staff/support check is re-run at delivery because registration is
    only a point-in-time guard — a demoted-but-active owner must stop receiving
    the all-user PII firehose immediately."""
    return (
        logging_models.EventConsumer.objects.filter(
            scopes__isnull=True,  # no bindings = global
            queue_created=True,
            user__is_active=True,
        )
        .filter(Q(user__is_staff=True) | Q(user__is_support=True))
        .exclude(rmq_username="")
        .select_related("user")
    )


class DispatchResult(NamedTuple):
    """Messages to publish, plus the owners of the consumers actually delivered to.

    ``user_ids`` is used by the marketplace caller to suppress the legacy path
    for users already on the unified path (no double-delivery). It therefore
    reflects only consumers that were REALLY sent this message — i.e. after the
    per-consumer ``object_types`` filter. Suppressing on mere entitlement would
    black-hole the event for a user whose unified consumer filters this type out
    but whose legacy subscription still wants it: dropped on both paths, with no
    error and no log.
    """

    messages: list[dict]
    user_ids: set[int]


def build_messages(
    scope_keys,
    payload_builder,
    object_type: ObservableObjectType,
    event_type: str | None = None,
    include_global: bool = False,
) -> DispatchResult:
    """Match consumers against an event's scope-keys and build its messages.

    ``scope_keys`` is the set of ``(content_type_id, object_id)`` pairs the event
    belongs to — the entity **and its ancestors** (offering, its customer, the
    project, the project's customer). Matching is a set intersection against the
    indexed ``EventConsumerScope`` table rather than a permission query per
    recipient, which is what makes fan-out O(matches).

    Delivery authorization (the security boundary) is preserved and dynamic:
      * bound consumer  -> owner must still hold an active role somewhere in
        this event's scope chain (ONE batched query for the whole fan-out), or
        be staff/support;
      * global consumer -> owner must still be staff/support.
    Registration validated the binding; this re-check makes revocation effective
    immediately, exactly as the per-recipient ``filter_for_user`` did before.

    ``payload_builder`` is a zero-arg callable invoked only when at least one
    consumer matches, so a system with no consumers pays nothing.

    ``include_global`` opts bindingless (global) consumers into the match. The
    core path (``dispatch_global_event``) always sets it; the marketplace path
    sets it for every object type except USER_ROLE — see the module docstring.
    """
    consumers = {}

    if scope_keys:
        matched_ids = (
            logging_models.EventConsumerScope.objects.filter(
                permission_utils.scope_keys_q(scope_keys)
            )
            .values_list("consumer_id", flat=True)
            .distinct()
        )
        for consumer in (
            # matched_ids stays a queryset (no list()) so this compiles to a
            # single query with a nested IN (SELECT ...) subquery rather than two
            # round trips — this runs on essentially every marketplace event.
            logging_models.EventConsumer.objects.filter(
                id__in=matched_ids, queue_created=True, user__is_active=True
            )
            .exclude(rmq_username="")
            .select_related("user")
        ):
            consumers[consumer.id] = consumer

    if include_global:
        for consumer in _global_consumers():
            consumers[consumer.id] = consumer

    if not consumers:
        return DispatchResult([], set())

    # Batched delivery re-auth for the BOUND consumers (globals already passed
    # the staff/support filter above).
    bound = [
        c for c in consumers.values() if not (c.user.is_staff or c.user.is_support)
    ]
    if bound:
        # A non-privileged recipient must hold a live role somewhere in the
        # event's scope chain. With no scope_keys there is nothing that could
        # authorize them, so allowed stays empty and they are all dropped —
        # a single branch covering both cases (no dead elif to mislead a reader).
        allowed_user_ids = (
            permission_utils.users_with_role_on_any_scope_key(
                {c.user_id for c in bound}, scope_keys
            )
            if scope_keys
            else set()
        )
        # Self-referential user scope: identity authorizes delivery — the
        # consumer's owner IS the affected user (a role can never grant this).
        # is_active is already enforced by the consumer queryset above.
        identity_allowed_user_ids = set()
        if scope_keys:
            user_ct_id = _user_ct_id()
            identity_allowed_user_ids = {
                object_id for ct_id, object_id in scope_keys if ct_id == user_ct_id
            }
        for consumer in bound:
            if (
                consumer.user_id not in allowed_user_ids
                and consumer.user_id not in identity_allowed_user_ids
            ):
                consumers.pop(consumer.id, None)

    if not consumers:
        return DispatchResult([], set())

    envelope = dict(payload_builder())
    envelope["object_type"] = object_type.value
    if event_type is not None:
        envelope["event_type"] = event_type
    envelope.setdefault("schema_version", 1)
    # DjangoJSONEncoder, not the stdlib default: payloads carry raw model values
    # (e.g. a DateField in the profile diff, a Decimal in a marketplace payload).
    # A TypeError here would surface INSIDE a post_save handler and abort the
    # user's save, so the encoder must handle date/datetime/Decimal/UUID.
    payload_str = json.dumps(envelope, cls=DjangoJSONEncoder)

    messages = []
    delivered_user_ids = set()
    for consumer in consumers.values():
        if consumer.object_types and object_type.value not in consumer.object_types:
            continue
        delivered_user_ids.add(consumer.user_id)
        messages.append(
            {
                "vhost": consumer.user.uuid.hex,
                "topic": consumer.queue_name,
                "payload": payload_str,
            }
        )
    return DispatchResult(messages, delivered_user_ids)


def dispatch_global_event(
    payload_builder,
    object_type: ObservableObjectType,
    event_type: str | None = None,
) -> None:
    """Fire-and-forget delivery of a user-centric event to global consumers."""
    result = build_messages(
        [], payload_builder, object_type, event_type, include_global=True
    )
    if result.messages:
        logging_tasks.publish_messages.delay(result.messages)


def _user_ct_id() -> int:
    # Lazy import: this module deliberately keeps its import surface minimal
    # (see the module docstring); ContentType.get_for_model is cached.
    from waldur_core.core.models import User

    return ContentType.objects.get_for_model(User).id


def dispatch_user_event(
    affected_user,
    payload_builder,
    object_type: ObservableObjectType,
    event_type: str | None = None,
) -> None:
    """Deliver a user-centric event to global consumers AND to the affected
    user's self-bound consumers (the self-referential ``user`` scope).

    Global consumers keep receiving everything exactly as with
    ``dispatch_global_event``; the ``(user_ct, user_id)`` scope-key
    additionally matches consumers bound to the affected user, authorized by
    identity rather than by role in ``build_messages``.
    """
    result = build_messages(
        [(_user_ct_id(), affected_user.id)],
        payload_builder,
        object_type,
        event_type,
        include_global=True,
    )
    if result.messages:
        logging_tasks.publish_messages.delay(result.messages)


# --- Emitters (signal handlers) -------------------------------------------
# Each is connected in logging/apps.py ready(). The get_skip_side_effects()
# guard suppresses firing during migrations, bulk loads, and imports. Cheap
# in-memory change detection (via the pre_save-stamped _old_values) happens
# BEFORE dispatch; the full payload is built lazily, only when a consumer
# exists.


def emit_user_profile(sender, instance, created=False, **kwargs):
    if get_skip_side_effects() or created:
        return
    old = getattr(instance, "_old_values", None)
    if not old:
        return

    # Data minimization / GDPR: only broadcast profile attributes the platform
    # currently exposes. The watched set is derived from
    # ENABLED_USER_PROFILE_ATTRIBUTES (+ always-on core fields) intersected with
    # the real model columns present, so an operator who disables a field (e.g.
    # phone_number, civil_number) keeps its changes from ever leaving the
    # platform, and newly-enabled fields are picked up automatically. Non-profile
    # auth/security columns are never eligible.
    watched = (get_enabled_profile_attributes() & set(old.keys())) - _NON_PROFILE_FIELDS
    changed = {}
    for field in watched:
        old_value = old.get(field)
        new_value = getattr(instance, field, None)
        if old_value != new_value:
            changed[field] = [old_value, new_value]
    if not changed:
        return
    dispatch_user_event(
        instance,
        lambda: {
            "user_uuid": _hex(instance.uuid),
            "user_username": instance.username,
            "email": instance.email,
            "full_name": instance.full_name,
            "changed": changed,
        },
        ObservableObjectType.USER_PROFILE,
        event_type="user_profile_updated",
    )


def emit_user_lifecycle(sender, instance, created=False, **kwargs):
    if get_skip_side_effects():
        return
    if created:
        action = "created"
    else:
        old = getattr(instance, "_old_values", None)
        if not old or old.get("is_active") == instance.is_active:
            return
        action = "activated" if instance.is_active else "deactivated"
    dispatch_user_event(
        instance,
        lambda: {
            "user_uuid": _hex(instance.uuid),
            "user_username": instance.username,
            "email": instance.email,
            "full_name": instance.full_name,
            "is_active": instance.is_active,
            "action": action,
        },
        ObservableObjectType.USER_LIFECYCLE,
        event_type=f"user_{action}",
    )


def emit_user_lifecycle_delete(sender, instance, **kwargs):
    if get_skip_side_effects():
        return
    dispatch_user_event(
        instance,
        lambda: {
            "user_uuid": _hex(instance.uuid),
            "user_username": instance.username,
            "email": instance.email,
            "full_name": instance.full_name,
            "action": "deleted",
        },
        ObservableObjectType.USER_LIFECYCLE,
        event_type="user_deleted",
    )


def _ssh_key_payload(instance, action):
    return {
        "user_uuid": _hex(instance.user.uuid),
        "user_username": instance.user.username,
        "ssh_key_uuid": _hex(instance.uuid),
        "name": instance.name,
        "fingerprint_sha256": instance.fingerprint_sha256,
        "public_key": instance.public_key,
        "action": action,
    }


def emit_user_ssh_key_save(sender, instance, created=False, **kwargs):
    if get_skip_side_effects():
        return
    action = "added" if created else "updated"
    dispatch_user_event(
        instance.user,
        lambda: _ssh_key_payload(instance, action),
        ObservableObjectType.USER_SSH_KEY,
        event_type=f"ssh_key_{action}",
    )


def emit_user_ssh_key_delete(sender, instance, **kwargs):
    if get_skip_side_effects():
        return
    dispatch_user_event(
        instance.user,
        lambda: _ssh_key_payload(instance, "removed"),
        ObservableObjectType.USER_SSH_KEY,
        event_type="ssh_key_removed",
    )


def _build_role_payload(permission, granted):
    scope = permission.scope
    scope_uuid = getattr(scope, "uuid", None)
    return {
        "user_uuid": _hex(permission.user.uuid),
        "user_username": permission.user.username,
        "email": permission.user.email,
        "full_name": permission.user.full_name,
        "role_name": permission.role.name,
        "granted": granted,
        "scope_type": permission.content_type.model,
        "scope_uuid": _hex(scope_uuid),
        "scope_name": getattr(scope, "name", None),
    }


def _dispatch_role_change(permission, granted):
    if get_skip_side_effects():
        return
    dispatch_user_event(
        permission.user,
        lambda: _build_role_payload(permission, granted),
        ObservableObjectType.USER_ROLE,
        event_type="role_granted" if granted else "role_revoked",
    )


def on_role_granted(sender, instance, **kwargs):
    _dispatch_role_change(instance, granted=True)


def on_role_revoked(sender, instance, **kwargs):
    _dispatch_role_change(instance, granted=False)
