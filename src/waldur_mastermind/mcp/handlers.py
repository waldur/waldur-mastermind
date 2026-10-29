from oauth2_provider.models import (
    get_access_token_model,
    get_grant_model,
    get_refresh_token_model,
)

from waldur_core.server.middleware import is_public_cors_path


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


def allow_public_cors(sender, request, **kwargs):
    """django-cors-headers: allow any origin on the OAuth endpoints."""
    return is_public_cors_path(request.path)
