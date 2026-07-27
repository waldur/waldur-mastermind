"""Classification of DRF ``request.auth`` values.

Deliberately imports no models: this is consumed by the permission layer and
the logging middleware, both of which load early enough that importing
``waldur_core.core.models`` here would create an import cycle. Classification
is therefore by type name rather than isinstance.
"""

AUTH_METHOD_SESSION = "session"
AUTH_METHOD_PAT = "pat"
AUTH_METHOD_TOKEN = "token"
AUTH_METHOD_OIDC = "oidc"
AUTH_METHOD_UNKNOWN = "unknown"


def get_auth_method(auth) -> str:
    """Return how the request was authenticated, given DRF's ``request.auth``."""
    if auth is None:
        return AUTH_METHOD_SESSION

    name = type(auth).__name__
    if name == "PersonalAccessToken":
        return AUTH_METHOD_PAT
    if name == "Token":
        return AUTH_METHOD_TOKEN
    if isinstance(auth, dict):
        return AUTH_METHOD_OIDC
    return AUTH_METHOD_UNKNOWN


def is_pat_auth(auth) -> bool:
    return get_auth_method(auth) == AUTH_METHOD_PAT
