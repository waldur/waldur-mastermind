from oauth2_provider.models import (
    get_access_token_model,
    get_grant_model,
    get_refresh_token_model,
)

from waldur_core.server.middleware import is_public_cors_path
from waldur_mastermind.mcp import models


def revoke_oauth_tokens_on_deactivation(sender, instance, created=False, **kwargs):
    """Delete a user's OAuth sessions when the user is deactivated.

    Refresh is refused for inactive users anyway, but issued access tokens would
    stay valid until they expire, and reactivating the user would revive them.
    """
    old_values = getattr(instance, "_old_values", None)
    if created or not old_values:
        return
    if old_values.get("is_active") and not instance.is_active:
        get_refresh_token_model().objects.filter(user=instance).delete()
        get_access_token_model().objects.filter(user=instance).delete()
        get_grant_model().objects.filter(user=instance).delete()


def forget_ended_connection(sender, instance, raw=False, **kwargs):
    """Before a new code, drop the record of the client's connection if it has
    ended, so the client is shown as authorised from now.

    The daily cleanup would only drop it later.
    """
    if instance._state.adding and not raw:
        models.ConnectedApp.objects.filter(
            user_id=instance.user_id, application_id=instance.application_id
        ).ended().delete()


def record_connected_app(sender, instance, created=False, raw=False, **kwargs):
    """Remember when the user first authorised the client, which DOT doesn't.

    Runs for new codes and new access tokens, so a connection whose record went
    missing, e.g. in a race with a revoke, is listed again from the next refresh.
    """
    # Not raw, from loaddata: the fixture brings its own records.
    if created and not raw and instance.user_id and instance.application_id:
        models.ConnectedApp.objects.get_or_create(
            user_id=instance.user_id, application_id=instance.application_id
        )


def allow_public_cors(sender, request, **kwargs):
    """django-cors-headers: allow any origin on the OAuth endpoints."""
    return is_public_cors_path(request.path)
