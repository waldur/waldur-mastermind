import reversion
from django.conf import settings
from django.contrib.auth.hashers import is_password_usable
from django.core.cache import cache
from django.db.models.fields.files import FieldFile
from django.forms import model_to_dict
from rest_framework.authtoken.models import Token

from waldur_core.core import utils as core_utils
from waldur_core.core.enums import CoreStates
from waldur_core.core.models import SshPublicKey, User
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.logging.middleware import get_event_context
from waldur_core.permissions.enums import RoleEnum
from waldur_core.structure.managers import get_connected_customers
from waldur_core.structure.models import Customer

# Fields rewritten by routine traffic (every login, every IdP sync) that carry no
# audit value on their own. They are still captured inside every snapshot - they
# just never open one, otherwise each login would produce a revision.
REVISION_IGNORED_USER_FIELDS = {
    "attribute_sources",
    "last_login",
    "last_sync",
    "modified",
    "password",
    # Denormalised search index recomputed from the name by User.save() on every
    # single save. It holds nothing first_name/last_name do not, and - since
    # normalize_unicode() preserves case - tracking it would reintroduce exactly
    # the IdP re-casing noise the case-insensitive comparison below filters out.
    "query_field",
}

# Name fields that federated IdPs are known to re-case between logins - e.g.
# eduTEAMS returning "JOHN SNOW" for a name the authoritative ISD owns as
# "John Snow" (see FEDERATED_IDENTITY_LOCKED_FIELDS in waldur_auth_social).
# The value is still written to the row; it just does not count as a change.
# Identity-bearing fields (username, email) are deliberately absent: a case
# change there is significant and must be recorded.
REVISION_CASE_INSENSITIVE_USER_FIELDS = {
    "first_name",
    "last_name",
    "native_name",
}


def get_field_snapshot(instance, exclude=()):
    """Return a comparable dict of an instance's own (non-m2m) field values."""
    meta = instance._meta
    excluded_fields = [field.name for field in meta.many_to_many]
    excluded_fields.append(meta.pk.name)
    excluded_fields.extend(exclude)
    return model_to_dict(instance, exclude=excluded_fields)


def _is_meaningful_change(field, old_value, new_value):
    old_value = _normalize(old_value)
    new_value = _normalize(new_value)
    if old_value == new_value:
        return False
    if field in REVISION_CASE_INSENSITIVE_USER_FIELDS:
        return _casefold(old_value) != _casefold(new_value)
    return True


def _normalize(value):
    """Make a value comparable across an in-memory instance and a fresh fetch."""
    if isinstance(value, FieldFile):
        # FieldFile compares by name, but an untouched file field carries None
        # in memory where a database round-trip yields "".
        return value.name or ""
    return value


def _casefold(value):
    if value is None:
        return ""
    return value.casefold() if isinstance(value, str) else value


def _get_revision_actor():
    """Resolve who to credit, as (author, note), if this is inside a request.

    The event context is populated per request and reset in
    CaptureEventContextMiddleware.process_response, so - unlike a bare
    thread-local - it cannot leak an actor across requests. Outside a request
    (login flows, Celery tasks, management commands) there is no actor and the
    revision is attributed by its comment instead.

    Under impersonation the accountable human is the impersonator, not the
    account being acted as: crediting the impersonated user would make a staff
    edit read as if that user had changed their own profile. The account being
    impersonated goes into the comment so the pair stays visible.
    """
    context = get_event_context() or {}
    impersonator_uuid = context.get("user_impersonator_uuid")
    actor_uuid = impersonator_uuid or context.get("user_uuid")
    if not actor_uuid:
        return None, None
    # all_objects, not objects: an actor deactivated mid-request would
    # otherwise silently drop off their own revision.
    author = User.all_objects.filter(uuid=actor_uuid).first()
    if not impersonator_uuid:
        return author, None
    return author, "impersonating {}".format(context.get("user_username") or "?")


def create_revision_on_update(sender, instance: User, created=False, **kwargs):
    """Snapshot a user whenever an audited field actually changes. See
    _create_revision_on_update; this wrapper only clears the one-shot marker."""
    try:
        _create_revision_on_update(instance, created)
    finally:
        # _change_source describes the save that just happened. Left in place it
        # would be inherited by the next save of the same in-memory instance,
        # mislabelling an unrelated change. Cleared here because this handler is
        # connected after log_user_save, the only other reader.
        instance.__dict__.pop("_change_source", None)


def _create_revision_on_update(instance: User, created: bool):
    """Snapshot a user whenever an audited field actually changes.

    User rows are written from a couple of dozen call sites - REST API, Django
    admin, OIDC/SAML2 login, SCIM, the Identity Bridge, invitation acceptance,
    role-driven (de)activation, management commands. Wrapping each of them in an
    explicit revision block does not scale and silently misses every new sync
    path, so history is recorded centrally here instead: pre_save has already
    stashed the previous values, and any save that changes something worth
    keeping opens a revision.
    """
    if created:
        # create_initial_revision already snapshots the newly created row.
        return
    if getattr(instance, "_skip_revision", False):
        return
    if reversion.is_active():
        # Already inside an explicit revision block - the Django admin change
        # form (VersionAdmin) or OIDC account adoption. Let it own the snapshot
        # so a single logical change does not produce two versions. Nothing is
        # lost: reversion's own post_save receiver adds the instance to that
        # revision.
        return
    old_values = getattr(instance, "_old_values", None)
    if not old_values:
        return

    # Deferred fields are excluded rather than compared: reading one would
    # trigger a query per field, and a field that was never loaded cannot have
    # been assigned in memory, so it cannot be what changed.
    new_values = get_field_snapshot(instance, exclude=instance.get_deferred_fields())
    changed_fields = sorted(
        field
        for field, old_value in old_values.items()
        if field not in REVISION_IGNORED_USER_FIELDS
        and field in new_values
        and _is_meaningful_change(field, old_value, new_values[field])
    )
    if not changed_fields:
        return

    with reversion.create_revision():
        reversion.add_to_revision(instance)
        author, impersonation = _get_revision_actor()
        if author is not None:
            reversion.set_user(author)
        annotations = []
        change_source = getattr(instance, "_change_source", None)
        if change_source:
            annotations.append(f"source: {change_source}")
        if impersonation:
            annotations.append(impersonation)
        comment = "Changed: {}".format(", ".join(changed_fields))
        if annotations:
            comment = "{} ({})".format(comment, "; ".join(annotations))
        reversion.set_comment(comment)


def create_initial_revision(sender, instance, created=False, **kwargs):
    """Create an initial reversion snapshot when an object is first created.

    This ensures that the history API returns the initial state of the object,
    rather than only recording changes from the first update onward.
    """
    if not created:
        return
    if not reversion.is_registered(sender):
        return
    with reversion.create_revision():
        reversion.add_to_revision(instance)
        reversion.set_comment("Initial version")


def create_auth_token(sender, instance: User, created=False, **kwargs):
    """Create a token for a new user."""
    if created:
        Token.objects.create(user=instance)


def preserve_fields_before_update(sender, instance: User, **kwargs):
    """Preserve fields of a user instance before it is updated."""
    if instance.pk is None:
        return

    old_instance = instance._meta.model.all_objects.get(pk=instance.pk)
    setattr(instance, "_old_values", get_field_snapshot(old_instance))


def delete_error_message(sender, instance, name, source, target, **kwargs):
    """Delete error message if instance state changed from erred"""
    if source != CoreStates.ERRED:
        return
    instance.error_message = ""
    instance.save(update_fields=["error_message"])


def set_default_token_lifetime(sender, instance: User, created=False, **kwargs):
    """Set the default token lifetime for a new user."""
    # Skip if token_lifetime was explicitly set (e.g., during import)
    if getattr(instance, "_token_lifetime_explicitly_set", False):
        return
    if created and instance.token_lifetime is None:
        # if settings used directly in model - django creates new migration every time settings change
        # Therefore - set default token_lifetime value in handler.
        if settings.WALDUR_CORE["TOKEN_LIFETIME"]:
            seconds = settings.WALDUR_CORE["TOKEN_LIFETIME"].total_seconds()
            instance.token_lifetime = int(seconds)
            # Applying the system default is part of creating the user, not a
            # change to it: the initial revision must stay the only one.
            instance._skip_revision = True
            try:
                instance.save(update_fields=["token_lifetime"])
            finally:
                instance._skip_revision = False


def log_user_save(sender, instance: User, created=False, **kwargs):
    """Log user creation, update, and activation/deactivation events."""
    if created:
        event_logger.emit(
            "User {affected_user_username} has been created.",
            event_type=EventType.USER_CREATION_SUCCEEDED,
            event_context={"affected_user": instance},
            scopes=[instance],
        )
    else:
        old_values = instance._old_values

        password_changed = (
            is_password_usable(old_values["password"])
            and instance.password != old_values["password"]
        )
        activation_changed = instance.is_active != old_values["is_active"]
        token_lifetime_changed = bool(
            old_values["token_lifetime"]
            and instance.token_lifetime != old_values["token_lifetime"]
        )
        user_details_changed = instance.details != old_values["details"]
        user_updated = any(
            old_value != getattr(instance, field_name)
            for field_name, old_value in old_values.items()
            if field_name in User.WHITELIST_FIELDS
        )

        if password_changed:
            event_logger.emit(
                "Password has been changed for user {affected_user_username}.",
                event_type=EventType.USER_PASSWORD_UPDATED,
                event_context={"affected_user": instance},
                scopes=[instance],
            )

        if activation_changed:
            if instance.is_active:
                event_logger.emit(
                    "User {affected_user_username} has been activated.",
                    event_type=EventType.USER_ACTIVATED,
                    event_context={"affected_user": instance},
                    scopes=[instance],
                )
            else:
                event_logger.emit(
                    "User {affected_user_username} has been deactivated.",
                    event_type=EventType.USER_DEACTIVATED,
                    event_context={"affected_user": instance},
                    scopes=[instance],
                )

        if token_lifetime_changed:
            event_logger.emit(
                "Token lifetime has been changed for {affected_user_username} to {affected_user_token_lifetime}",
                event_type=EventType.TOKEN_LIFETIME_UPDATED,
                event_context={"affected_user": instance},
                scopes=[instance],
            )

        if user_details_changed:
            event_logger.emit(
                "Details for {{affected_user_username}} have been updated from {} to {}.".format(
                    str(old_values["details"])
                    .strip("{}")
                    .replace("{", "{{")
                    .replace("}", "}}"),
                    str(instance.details)
                    .strip("{}")
                    .replace("{", "{{")
                    .replace("}", "}}"),
                ),
                event_type=EventType.USER_DETAILS_UPDATE_SUCCEEDED,
                event_context={"affected_user": instance},
                scopes=[instance],
            )

        if user_updated:
            diff = [
                f"{field_name}: {old_value} -> {getattr(instance, field_name)}"
                for field_name, old_value in old_values.items()
                if field_name in User.WHITELIST_FIELDS
                and old_value != getattr(instance, field_name)
            ]

            change_source = getattr(instance, "_change_source", None)
            source_suffix = f" Source: {change_source}." if change_source else ""

            # Escape braces in diff values to avoid .format() interpretation
            safe_diff = "\n".join(
                line.replace("{", "{{").replace("}", "}}") for line in diff
            )

            event_logger.emit(
                "User {affected_user_username} has been updated.%s Details:\n%s"
                % (source_suffix, safe_diff),
                event_type=EventType.USER_UPDATE_SUCCEEDED,
                event_context={"affected_user": instance},
                scopes=[instance],
            )

            organizations = get_connected_customers(instance, RoleEnum.CUSTOMER_OWNER)

            if (
                organizations.exists()
                and settings.WALDUR_CORE["NOTIFICATIONS_PROFILE_CHANGES"][
                    "ENABLE_OPERATOR_OWNER_NOTIFICATIONS"
                ]
                and settings.WALDUR_CORE["NOTIFICATIONS_PROFILE_CHANGES"][
                    "OPERATOR_NOTIFICATION_EMAILS"
                ]
            ):
                # We add the fields that have changed to the context for the notification email
                fields = []
                for field_name, old_value in old_values.items():
                    if field_name in User.WHITELIST_FIELDS and old_value != getattr(
                        instance, field_name
                    ):
                        fields.append(
                            {
                                "name": field_name,
                                "old_value": old_value,
                                "new_value": getattr(instance, field_name),
                            }
                        )

                context = {
                    "user": instance,
                    "fields": fields,
                    "organizations": Customer.objects.filter(id__in=organizations),
                }

                emails = settings.WALDUR_CORE["NOTIFICATIONS_PROFILE_CHANGES"][
                    "OPERATOR_NOTIFICATION_EMAILS"
                ]
                core_utils.broadcast_mail(
                    "structure",
                    "notifications_profile_changes_operator",
                    context,
                    emails,
                )


def log_user_delete(sender, instance: User, **kwargs):
    """Log user deletion events."""
    event_logger.emit(
        "User {affected_user_username} has been deleted.",
        event_type=EventType.USER_DELETION_SUCCEEDED,
        event_context={"affected_user": instance},
        scopes=[instance],
    )


def log_ssh_key_save(sender, instance: SshPublicKey, created=False, **kwargs):
    """Log SSH key creation events."""
    if created:
        event_logger.emit(
            "SSH key {ssh_key_name} has been created for user%s with username {user_username}."
            % (" {user_full_name}" if instance.user.full_name else ""),
            event_type=EventType.SSH_KEY_CREATION_SUCCEEDED,
            event_context={"ssh_key": instance, "user": instance.user},
            scopes=[instance.user],
        )


def log_ssh_key_delete(sender, instance: SshPublicKey, **kwargs):
    """Log SSH key deletion events."""
    event_logger.emit(
        "SSH key {ssh_key_name} has been deleted for user%s with username {user_username}."
        % (" {user_full_name}" if instance.user.full_name else ""),
        event_type=EventType.SSH_KEY_DELETION_SUCCEEDED,
        event_context={"ssh_key": instance, "user": instance.user},
        scopes=[instance.user],
    )


def log_token_create(sender, instance: Token, created=False, **kwargs):
    """Log token creation events."""
    if created:
        event_logger.emit(
            "Token has been updated for {affected_user_username}",
            event_type=EventType.TOKEN_CREATED,
            event_context={"affected_user": instance.user},
            scopes=[instance.user],
        )


def revoke_user_pats_on_deactivation(sender, instance: User, **kwargs):
    """Revoke all active PATs when a user is deactivated."""
    if instance.pk is None:
        return
    old_values = getattr(instance, "_old_values", None)
    if old_values is None:
        return
    # Only act when is_active changes from True to False
    if old_values.get("is_active") and not instance.is_active:
        count = instance.personal_access_tokens.filter(is_active=True).update(
            is_active=False
        )
        if count:
            from waldur_core.logging import event_logger as _event_logger
            from waldur_core.logging.enums import EventType

            _event_logger.emit(
                f"All personal access tokens ({count}) for user {{affected_user_username}} "
                "have been revoked due to user deactivation.",
                event_type=EventType.PAT_REVOKED,
                event_context={"affected_user": instance},
                scopes=[instance],
            )


def emit_user_blocked_event(username, ip_address, dedup_timeout):
    """Emit a user_blocked event at most once per (username, IP) lockout window.

    Both lockout paths call this: the django-axes signal and the token-auth
    failure counter. django-axes re-sends user_locked_out on every attempt made
    while a lockout is already in force, so without a guard a single lockout
    would log one event per attack request. cache.add is atomic, so it also
    collapses the two paths (and concurrent requests) into a single event.

    The lockout may not correspond to an existing user (the username can be
    unknown), so the event carries the raw username and IP rather than a user
    scope, mirroring the failed-login event.
    """
    username = username or ""
    ip_address = ip_address or ""
    dedup_key = f"USER_BLOCKED_EMITTED_OF_{username}_AT_{ip_address}"
    if not cache.add(dedup_key, True, dedup_timeout):
        return
    event_logger.emit(
        "User {username} has been blocked after too many failed login attempts "
        "from {ip_address}.",
        event_type=EventType.USER_BLOCKED,
        event_context={"username": username, "ip_address": ip_address},
        scopes=[],
    )


def log_user_locked_out(sender, username=None, ip_address=None, request=None, **kwargs):
    """Emit a user_blocked event when django-axes locks out a username/IP."""
    # AXES_COOLOFF_TIME may be a timedelta, an int (hours), or None (no cool-off).
    cooloff = settings.AXES_COOLOFF_TIME
    if hasattr(cooloff, "total_seconds"):
        dedup_timeout = cooloff.total_seconds()
    elif cooloff:
        dedup_timeout = cooloff * 3600
    else:
        dedup_timeout = 600
    emit_user_blocked_event(username, ip_address, dedup_timeout)


def constance_updated(sender, key, old_value, new_value, **kwargs):
    """Clear the API configuration cache when a Constance setting is updated."""
    cache.delete("API_CONFIGURATION")
