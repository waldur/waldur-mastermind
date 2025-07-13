from waldur_core.core.models import User


def update_registration_method(sender, instance: User, **kwargs):
    """Update user's registration method to SAML2."""
    user = instance
    if user.registration_method != "SAML2":
        user.registration_method = "SAML2"
        return True
    else:
        return False
