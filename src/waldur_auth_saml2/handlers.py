def update_registration_method(sender, instance, **kwargs):
    user = instance
    if user.registration_method != 'SAML2':
        user.registration_method = 'SAML2'
        return True
    else:
        return False
