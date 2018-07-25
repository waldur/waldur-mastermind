from __future__ import unicode_literals

from django.contrib.auth.decorators import login_required

from waldur_core.core import views

from .log import event_logger


@login_required
def login_completed(request):
    """
    Callback view called after user has successfully logged in.
    Redirects user agent to frontend view with valid token as hash parameter.
    """
    token = views.RefreshTokenMixin().refresh_token(request.user)
    event_logger.auth_openid.info(
        'User {user_full_name} authenticated successfully with eID.',
        event_type='auth_logged_in_with_openid',
        event_context={'user': request.user}
    )
    return views.login_completed(token.key, 'openid')


def login_failed(request, message):
    return views.login_failed(message)
