from drf_spectacular.authentication import SessionScheme, TokenScheme


class WaldurTokenScheme(TokenScheme):
    target_class = "waldur_core.core.authentication.TokenAuthentication"
    name = "waldurTokenAuth"


class WaldurSessionScheme(SessionScheme):
    target_class = "waldur_core.core.authentication.SessionAuthentication"
    name = "waldurCookieAuth"
