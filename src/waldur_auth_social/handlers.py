from waldur_auth_social.revocation import forget_oauth_tokens


def forget_oauth_tokens_on_deactivation(sender, instance, **kwargs):
    """Revoke the identity provider tokens of a user being deactivated."""
    if instance.pk is None:
        return
    old_values = getattr(instance, "_old_values", None)
    if old_values is None:
        return
    if old_values.get("is_active") and not instance.is_active:
        forget_oauth_tokens(instance)


def forget_oauth_tokens_on_deletion(sender, instance, **kwargs):
    """Revoke them before the row goes; the cascade would only delete them."""
    forget_oauth_tokens(instance)
