from urllib.parse import urlencode

from waldur_auth_social.models import IdentityProvider
from waldur_auth_social.revocation import RevocationReason, forget_oauth_tokens
from waldur_core.configuration import config
from waldur_core.core.utils import format_homeport_link


def forget_oauth_tokens_on_logout(user):
    """Revoke the identity provider tokens of a user logging out."""
    forget_oauth_tokens(user, RevocationReason.LOGOUT)


def get_oidc_logout_url(authentication_method: str) -> str | None:
    """Resolve the OIDC post-logout redirect URL for ``authentication_method``."""
    try:
        idp = IdentityProvider.objects.get(provider=authentication_method)
    except IdentityProvider.DoesNotExist:
        return None
    if idp.logout_url and idp.enable_post_logout_redirect:
        params = {
            "post_logout_redirect_uri": format_homeport_link("login/"),
            "client_id": idp.client_id,
        }
        return f"{idp.logout_url}?{urlencode(params)}"
    if config.OIDC_DEFAULT_LOGOUT_URL:
        # Fallback to default logout URL if IdentityProvider doesn't support OIDC logout
        return config.OIDC_DEFAULT_LOGOUT_URL
    return None


def get_identity_provider(registration_method: str) -> IdentityProvider | None:
    """Return the configured IdentityProvider row for ``registration_method``."""
    return IdentityProvider.objects.filter(provider=registration_method).first()


def get_default_idp_info(provider_name: str) -> dict | None:
    """Return public configuration metadata for the active default IdP."""
    try:
        provider = IdentityProvider.objects.get(provider=provider_name, is_active=True)
    except IdentityProvider.DoesNotExist:
        return None
    return {
        "provider": provider.provider,
        "client_id": provider.client_id,
        "auth_url": provider.auth_url,
    }


def forget_oauth_tokens_on_deactivation(sender, instance, **kwargs):
    """Revoke the identity provider tokens of a user being deactivated."""
    if instance.pk is None:
        return
    old_values = getattr(instance, "_old_values", None)
    if old_values is None:
        return
    if old_values.get("is_active") and not instance.is_active:
        forget_oauth_tokens(instance, RevocationReason.DEACTIVATED)


def forget_oauth_tokens_on_deletion(sender, instance, **kwargs):
    """Revoke them before the row goes; the cascade would only delete them."""
    forget_oauth_tokens(instance, RevocationReason.DELETED)
