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
from collections.abc import Callable
from typing import Any, NamedTuple

from celery import shared_task
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Q
from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_core.core.middleware import get_skip_side_effects
from waldur_core.core.user_attributes import get_enabled_profile_attributes
from waldur_core.logging import models as logging_models
from waldur_core.logging import tasks as logging_tasks
from waldur_core.logging.enums import ObservableObjectType
from waldur_core.permissions import models as permission_models
from waldur_core.permissions import utils as permission_utils
from waldur_core.structure import permissions as structure_permissions

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
            deliverable_q(),
            scopes__isnull=True,  # no bindings = global
            user__is_active=True,
        )
        .filter(Q(user__is_staff=True) | Q(user__is_support=True))
        .select_related("user")
    )


def deliverable_q() -> Q:
    """Consumers with a working transport: a provisioned queue, or a webhook URL."""
    Delivery = logging_models.EventConsumer.Delivery
    return (Q(delivery=Delivery.RMQ, queue_created=True) & ~Q(rmq_username="")) | (
        Q(delivery=Delivery.WEBHOOK) & ~Q(webhook_url="")
    )


def consumer_message(consumer, payload_str: str) -> dict:
    """One publishable message for ``consumer``, shaped for its transport.

    ``publish_messages`` routes on the shape: ``webhook_consumer`` goes to the
    HTTP delivery task, ``vhost``/``topic`` to STOMP.
    """
    if consumer.is_webhook:
        return {"webhook_consumer": consumer.uuid.hex, "payload": payload_str}
    return {
        "vhost": consumer.user.uuid.hex,
        "topic": consumer.queue_name,
        "payload": payload_str,
    }


def _envelope(payload, object_type: ObservableObjectType, event_type) -> dict:
    envelope = dict(payload)
    envelope["object_type"] = object_type.value
    if event_type is not None:
        envelope["event_type"] = event_type
    envelope.setdefault("schema_version", 1)
    # Delivery is at-least-once with no ordering across events (each event is
    # its own task, retried independently), so a receiver needs the emission
    # time to discard an update older than one it already applied.
    envelope.setdefault("emitted_at", timezone.now().isoformat())
    return envelope


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
                deliverable_q(), id__in=matched_ids, user__is_active=True
            ).select_related("user")
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

    envelope = _envelope(payload_builder(), object_type, event_type)
    # DjangoJSONEncoder, not the stdlib default: payloads carry raw model values
    # (e.g. a DateField in the profile diff, a Decimal in a marketplace payload).
    # A TypeError here would surface INSIDE a post_save handler and abort the
    # user's save, so the encoder must handle date/datetime/Decimal/UUID.
    payload_str = json.dumps(envelope, cls=DjangoJSONEncoder)

    messages = []
    delivered_user_ids = set()
    for consumer in consumers.values():
        if not consumer.accepts(object_type.value, envelope):
            continue
        # Only a queue counts as "this user already has it": the marketplace
        # caller skips the user's legacy subscription queues on this set, and a
        # webhook delivers to some other system, not to the user's own client.
        if not consumer.is_webhook:
            delivered_user_ids.add(consumer.user_id)
        messages.append(consumer_message(consumer, payload_str))
    return DispatchResult(messages, delivered_user_ids)


# Scope chains that differ from get_scope_ancestors, keyed by model class. An app
# whose events are scoped to a chain the generic walker does not build registers
# that chain in its AppConfig.ready(), and passes the same function's result to
# build_messages. Registration (holds_role_on_event_chain) and the diagnostic
# (delivery_blocked_reason) then read the chain from here, so all three agree
# and this module still imports no app code.
_event_chains: dict[type, Callable[[Any], list]] = {}


def register_event_chain(model: type, chain: Callable[[Any], list]) -> None:
    """Register ``chain(instance) -> [instance, *ancestors]`` for ``model``."""
    _event_chains[model] = chain


def event_scope_keys(scope) -> list[tuple[int, int]]:
    """Scope keys of the chain an event about ``scope`` is emitted with."""
    chain = _event_chains.get(type(scope))
    if chain is None:
        return permission_utils.scope_keys_for(scope)
    return [
        (ContentType.objects.get_for_model(entity.__class__).id, entity.id)
        for entity in chain(scope)
    ]


def holds_role_on_event_chain(user, scope) -> bool:
    """Registration guard: may this user bind a consumer to ``scope``?

    For a scope with a registered chain, the role must lie on that chain -- the
    one build_messages re-authorizes delivery against -- so a binding is accepted
    exactly when it could receive something. Anything else keeps the generic
    ancestor walk.
    """
    if type(scope) not in _event_chains:
        return permission_utils.holds_any_role_on_scope_or_ancestor(user, scope)
    if not user.is_active:
        return False
    if user.is_staff or user.is_support:
        return True
    return bool(
        permission_utils.users_with_role_on_any_scope_key(
            {user.id}, event_scope_keys(scope)
        )
    )


_OBSERVABLE_OBJECT_TYPE_VALUES = frozenset(
    member.value for member in ObservableObjectType
)


def delivery_blocked_reason(consumer) -> str | None:
    """Why nothing at all can reach this consumer, or None if something can.

    Kept next to :func:`build_messages` so the two cannot drift: the ladder
    below walks the drop conditions in the order the dispatcher applies them —
    the candidate querysets (``user__is_active``, and per transport either
    ``queue_created`` with a non-empty ``rmq_username`` or a non-empty
    ``webhook_url``), the staff/support bypass, the global-consumer
    staff/support filter, and finally the batched role re-check.

    The last rung is deliberately WIDER than dispatch: it asks whether the owner
    holds a role anywhere in the chain of *any* binding, rather than against one
    event's scope-keys. So an empty answer means "something is being delivered",
    not "everything is".

    Each binding contributes the chain its events are emitted with
    (:func:`event_scope_keys`), so this is the standalone registration guard
    (:func:`holds_role_on_event_chain`, which the serializer applies) — a proposal
    binding resolves to the call side, not the applicant's project. It is NOT the
    site-agent guard: ``register_queue``
    also admits an identity manager, who holds no role on the offering at all.
    Such a consumer is reported blocked here — correctly, since dispatch drops
    it too — which is why the message says the owner holds no role rather than
    that they lost one.

    ``object_types`` is only half-modelled, and deliberately so. Dispatch drops
    a message per event when the type is outside the allow-list; whether a given
    consumer will ever see a matching event cannot be decided from the row. What
    *can* be decided is an allow-list none of whose entries is a live
    :class:`ObservableObjectType` — a type that was renamed or removed out from
    under a stored row — which matches nothing by construction.
    """
    user = consumer.user
    if not user.is_active:
        return "Owner account is deactivated."
    if consumer.is_webhook:
        if not consumer.webhook_url:
            return "Webhook consumer has no destination URL."
    elif not consumer.queue_created:
        return "Queue is not provisioned in RabbitMQ."
    elif not consumer.rmq_username:
        return "Consumer has no RabbitMQ credential."
    # Before the staff/support bypass: a dead type filter drops the message
    # after authorization, so privilege does not rescue it.
    if consumer.object_types and not any(
        object_type in _OBSERVABLE_OBJECT_TYPE_VALUES
        for object_type in consumer.object_types
    ):
        return (
            "None of the object types this consumer filters on still exists, "
            "so every event is dropped by the filter."
        )
    if user.is_staff or user.is_support:
        return None

    scopes = list(consumer.scopes.all())
    if not scopes:
        return (
            "Owner is no longer staff/support, so this global consumer "
            "receives nothing."
        )

    # Self-referential user bindings are authorized by identity, not by a role,
    # exactly as in build_messages. Everything else contributes its scope chain
    # to ONE role query, rather than a query per binding.
    user_ct_id = _user_ct_id()
    role_keys = set()
    resolvable = False
    for binding in scopes:
        if binding.content_type_id == user_ct_id:
            if binding.object_id == user.id:
                return None
            # Someone else's identity: no role can ever authorize it, so it
            # contributes no scope keys — but it IS a live binding, and leaving
            # it out of `resolvable` would report a consumer bound only to other
            # users (a staff registration whose owner was later demoted) as
            # pointing at rows that no longer exist.
            resolvable = True
            continue
        scope = binding.scope
        if scope is not None:
            resolvable = True
            role_keys.update(event_scope_keys(scope))
    if not resolvable:
        # Every binding is a dangling GenericFK — the bound project/offering row
        # was deleted. Distinct from a role problem: the fix is to re-register,
        # not to restore a role.
        return "The entities this consumer is bound to no longer exist."
    if permission_utils.users_with_role_on_any_scope_key({user.id}, role_keys):
        return None
    return "Owner holds no role on any scope this consumer is bound to."


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


def _scope_customer(scope):
    """The organization a role scope belongs to, or None if it has none."""
    if scope is None or not hasattr(scope, "Permissions"):
        return None
    try:
        return structure_permissions._get_customer(scope)
    except (AttributeError, ObjectDoesNotExist):
        return None


_UNRESOLVED = object()


def _build_role_payload(permission, granted, customer=_UNRESOLVED):
    scope = permission.scope
    scope_uuid = getattr(scope, "uuid", None)
    # The organization is the natural join key for an external system mirroring
    # Waldur's structure: the scope itself for an organization role, the parent
    # organization for a project/offering/call role.
    if customer is _UNRESOLVED:
        customer = _scope_customer(scope)
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
        "customer_uuid": _hex(getattr(customer, "uuid", None)),
        "customer_name": getattr(customer, "name", None),
        "customer_registration_code": getattr(customer, "registration_code", None),
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


# --- Resync: replay current state to one consumer -------------------------
# Events only describe changes, so a receiver that starts late (or lost its
# state) has no way to learn what already exists. A resync sends the current
# state as snapshot messages through the consumer's own transport and filters.
# Lives here rather than in tasks.py because it builds the same payloads as the
# signal emitters above, and this module already imports tasks.py.

SNAPSHOT_BATCH_SIZE = 200
RESYNC_OBJECT_TYPES = (ObservableObjectType.USER_ROLE,)


def role_snapshot_batches(consumer):
    """Messages describing every active role grant this consumer accepts.

    Yields lists of at most SNAPSHOT_BATCH_SIZE messages. The role-name and
    scope-type filters are pushed into the query, since they are the common
    case (e.g. organization owners only) and otherwise every grant on the
    platform would be loaded just to be dropped.
    """
    filters = consumer.payload_filters or {}
    roles = (
        permission_models.UserRole.objects.filter(is_active=True, user__is_active=True)
        .select_related("user", "role", "content_type")
        # One query per content type per chunk instead of one per grant.
        .prefetch_related("scope")
        .order_by("id")
    )
    if "role_name" in filters:
        roles = roles.filter(role__name__in=filters["role_name"])
    if "scope_type" in filters:
        roles = roles.filter(content_type__model__in=filters["scope_type"])

    object_type = ObservableObjectType.USER_ROLE
    # Many grants share an organization; resolve each scope's customer once.
    customers = {}
    batch = []
    # Client-side chunks, not .iterator(): a server-side cursor breaks behind
    # PgBouncer, and this generator suspends between fetches while its caller
    # publishes each batch.
    for permission in core_utils.chunked_queryset(roles, chunk_size=500):
        if permission.scope is None:  # dangling GenericFK
            continue
        key = (permission.content_type_id, permission.object_id)
        if key not in customers:
            customers[key] = _scope_customer(permission.scope)
        envelope = _envelope(
            _build_role_payload(permission, granted=True, customer=customers[key]),
            object_type,
            "role_snapshot",
        )
        if not consumer.accepts(object_type.value, envelope):
            continue
        payload_str = json.dumps(envelope, cls=DjangoJSONEncoder)
        batch.append(consumer_message(consumer, payload_str))
        if len(batch) >= SNAPSHOT_BATCH_SIZE:
            yield batch
            batch = []
    if batch:
        yield batch


@shared_task(name="waldur_core.logging.resync_event_consumer", is_background=True)
def resync_event_consumer(consumer_uuid: str) -> dict:
    """Send the current role state to a global consumer as snapshot messages."""
    consumer = (
        logging_models.EventConsumer.objects.filter(deliverable_q(), uuid=consumer_uuid)
        .select_related("user")
        .first()
    )
    # Re-checked here, not only in the view: a snapshot is the all-user PII
    # firehose, and the owner may have been demoted while the task was queued.
    if (
        consumer is None
        or not consumer.user.is_active
        or not (consumer.user.is_staff or consumer.user.is_support)
        or not consumer.is_global
    ):
        return {"sent": 0}

    sent = 0
    for batch in role_snapshot_batches(consumer):
        logging_tasks.publish_messages.delay(batch)
        sent += len(batch)
    logger.info("Resync queued %s snapshot messages for %s", sent, consumer)
    return {"sent": sent}
