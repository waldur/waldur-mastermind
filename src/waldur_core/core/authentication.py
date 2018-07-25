from __future__ import unicode_literals

from django.conf import settings
from django.utils import timezone
from django.utils.translation import ugettext_lazy as _
from rest_framework import exceptions
import rest_framework.authentication

import waldur_core.logging.middleware

TOKEN_KEY = settings.WALDUR_CORE.get('TOKEN_KEY', 'x-auth-token')


def can_access_admin_site(user):
    return user.is_active and (user.is_staff or user.is_support)


class AuthenticationBackend(object):
    """
    Enables only support and staff to access admin site.
    """

    def authenticate(self, request, username, password):
        """
        Always return ``None`` to prevent authentication within this backend.
        """
        return None

    def has_perm(self, user_obj, perm, obj=None):
        return can_access_admin_site(user_obj)

    def has_module_perms(self, user_obj, app_label):
        return can_access_admin_site(user_obj)


class TokenAuthentication(rest_framework.authentication.TokenAuthentication):
    """
    Custom token-based authentication.

    Use TOKEN_KEY from request query parameters if authentication token was not found in header.
    """

    def get_authorization_value(self, request):
        auth = rest_framework.authentication.get_authorization_header(request)
        if not auth:
            auth = request.query_params.get(TOKEN_KEY, '')
        return auth

    def authenticate_credentials(self, key):
        model = self.get_model()
        try:
            token = model.objects.select_related('user').get(key=key)
        except model.DoesNotExist:
            raise exceptions.AuthenticationFailed(_('Invalid token.'))

        if not token.user.is_active:
            raise exceptions.AuthenticationFailed(_('User inactive or deleted.'))

        if token.user.token_lifetime:
            lifetime = timezone.timedelta(seconds=token.user.token_lifetime)

            if token.created < timezone.now() - lifetime:
                raise exceptions.AuthenticationFailed(_('Token has expired.'))

        return token.user, token

    def authenticate(self, request):
        auth = self.get_authorization_value(request).split()

        if not auth or auth[0].lower() != b'token':
            return None

        if len(auth) == 1:
            msg = _('Invalid token. No credentials provided.')
            raise exceptions.AuthenticationFailed(msg)
        elif len(auth) > 2:
            msg = _('Invalid token. Token string should not contain spaces.')
            raise exceptions.AuthenticationFailed(msg)

        try:
            token = auth[1].decode()
        except UnicodeError:
            msg = _('Invalid token header. Token string should not contain invalid characters.')
            raise exceptions.AuthenticationFailed(msg)

        return self.authenticate_credentials(token)


def user_capturing_auth(auth):
    class CapturingAuthentication(auth):
        def authenticate(self, request):
            result = super(CapturingAuthentication, self).authenticate(request)
            if result is not None:
                user, _ = result
                waldur_core.logging.middleware.set_current_user(user)
                token = user.auth_token
                if token:
                    token.created = timezone.now()
                    token.save()
            return result

    return CapturingAuthentication


class CsrfExemptSessionAuthentication(rest_framework.authentication.SessionAuthentication):

    def enforce_csrf(self, request):
        return  # Skip CSRF check


SessionAuthentication = user_capturing_auth(CsrfExemptSessionAuthentication)
TokenAuthentication = user_capturing_auth(TokenAuthentication)
