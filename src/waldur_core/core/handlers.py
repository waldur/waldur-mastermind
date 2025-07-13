from django.conf import settings
from django.contrib.auth.hashers import is_password_usable
from django.core.cache import cache
from django.forms import model_to_dict
from rest_framework.authtoken.models import Token

from waldur_core.core import utils as core_utils
from waldur_core.core.enums import CoreStates
from waldur_core.core.models import SshPublicKey, User
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.permissions.enums import RoleEnum
from waldur_core.structure.managers import get_connected_customers
from waldur_core.structure.models import Customer


def create_auth_token(sender, instance: User, created=False, **kwargs):
    """Create a token for a new user."""
    if created:
        Token.objects.create(user=instance)


def preserve_fields_before_update(sender, instance: User, **kwargs):
    """Preserve fields of a user instance before it is updated."""
    if instance.pk is None:
        return

    meta = instance._meta
    old_instance = meta.model.all_objects.get(pk=instance.pk)

    excluded_fields = [field.name for field in meta.many_to_many]
    excluded_fields.append(meta.pk.name)
    old_values = model_to_dict(old_instance, exclude=excluded_fields)

    setattr(instance, "_old_values", old_values)


def delete_error_message(sender, instance, name, source, target, **kwargs):
    """Delete error message if instance state changed from erred"""
    if source != CoreStates.ERRED:
        return
    instance.error_message = ""
    instance.save(update_fields=["error_message"])


def set_default_token_lifetime(sender, instance: User, created=False, **kwargs):
    """Set the default token lifetime for a new user."""
    if created:
        # if settings used directly in model - django creates new migration every time settings change
        # Therefore - set default token_lifetime value in handler.
        if settings.WALDUR_CORE["TOKEN_LIFETIME"]:
            seconds = settings.WALDUR_CORE["TOKEN_LIFETIME"].total_seconds()
            instance.token_lifetime = int(seconds)
            instance.save(update_fields=["token_lifetime"])


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
                    str(old_values["details"]).strip("{}"),
                    str(instance.details).strip("{}"),
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

            event_logger.emit(
                "User {affected_user_username} has been updated. Details:\n%s"
                % "\n".join(diff),
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


def constance_updated(sender, key, old_value, new_value, **kwargs):
    """Clear the API configuration cache when a Constance setting is updated."""
    cache.delete("API_CONFIGURATION")
