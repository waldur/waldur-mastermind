"""
How Waldur authenticates as a client to an identity provider's token endpoints.

The endpoints needed to sign in are stored on ``IdentityProvider`` when it is
saved. Two other facts are read from the provider's discovery document when
needed and cached, so they follow the provider without an admin action or a
stored field:

- ``revocation_endpoint``, used to revoke a stored refresh token
  (``waldur_auth_social.revocation``);
- ``token_endpoint_auth_methods_supported``, which decides how the client
  credentials are sent to the token and revocation endpoints. Some providers
  (TARA, Unity IdM / EUDAT B2ACCESS) accept them only as an HTTP Basic
  Authorization header (``client_secret_basic``), most accept them in the
  request body too (``client_secret_post``).

A discovery document can be wrong, so a request the provider rejects with
``invalid_client`` is sent again with the other ways of authenticating, and
the one that worked is remembered for the provider.

HTTP Basic has two of them. RFC 6749 section 2.3.1 has the client id and
secret form-urlencoded before they are joined and base64-encoded, and servers
that follow it decode them; others (any Flask/werkzeug one, for instance) take
them as they are. Both forms are the same for a secret of letters, digits and
``-._~``; for any other secret both are tried.
"""

import base64
import hashlib
import logging
from urllib.parse import quote_plus

import requests
from django.core.cache import cache
from requests.auth import AuthBase

from .const import ProviderChoices, TokenEndpointAuthMethod
from .models import IdentityProvider

logger = logging.getLogger(__name__)

# Sign-in, token refresh and logout wait for the document when it is not cached.
REQUEST_TIMEOUT = 5

# The document rarely changes; one without a revocation endpoint is cached too,
# so it is not looked up again on every logout.
DISCOVERY_CACHE_TIMEOUT = 60 * 60

# The last document read is kept much longer, and used while the provider's
# discovery endpoint cannot be reached: a provider that accepts only HTTP Basic
# keeps working through a discovery outage.
LAST_KNOWN_CACHE_TIMEOUT = 30 * 24 * 60 * 60

# A document that could not be read is not asked for again for a minute, so an
# unreachable discovery endpoint does not hold up every request.
FAILURE_CACHE_TIMEOUT = 60

# How long a method that worked after the advertised one was rejected is kept.
REMEMBERED_METHOD_TIMEOUT = 30 * 24 * 60 * 60

# Only the keys read here are cached.
CACHED_KEYS = ("revocation_endpoint", "token_endpoint_auth_methods_supported")

# OpenID Connect Discovery 1.0: the default when the provider omits the list.
DEFAULT_AUTH_METHODS = [TokenEndpointAuthMethod.CLIENT_SECRET_BASIC]

# HTTP Basic without the RFC 6749 form-urlencoding, for servers that do not
# decode it. Not an OIDC value: it is only ever remembered, never advertised.
CLIENT_SECRET_BASIC_UNENCODED = "client_secret_basic_unencoded"

# The order the ways of authenticating are tried in after the first is rejected.
FALLBACK_ORDER = (
    TokenEndpointAuthMethod.CLIENT_SECRET_BASIC,
    CLIENT_SECRET_BASIC_UNENCODED,
    TokenEndpointAuthMethod.CLIENT_SECRET_POST,
)


class _UnencodedBasicAuth(AuthBase):
    """HTTP Basic with the credentials as they are, encoded as UTF-8.

    requests' own Basic encodes them as Latin-1, which fails outright for
    other characters and does not match what a UTF-8 server decodes.
    """

    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password

    def __eq__(self, other):
        return isinstance(other, _UnencodedBasicAuth) and (
            (self.username, self.password) == (other.username, other.password)
        )

    def __call__(self, request):
        credentials = f"{self.username}:{self.password}".encode()
        request.headers["Authorization"] = (
            "Basic " + base64.b64encode(credentials).decode()
        )
        return request


def _cache_key(kind: str, idp: IdentityProvider) -> str:
    digest = hashlib.sha256(idp.discovery_url.encode()).hexdigest()
    return f"oidc_{kind}:{idp.provider}:{digest}"


def get_discovery_document(idp: IdentityProvider) -> dict | None:
    """The parts of the provider's discovery document read at request time.

    While the document cannot be read, the last one read is returned, or
    ``None`` if there is none.
    """
    document = cache.get(_cache_key("discovery", idp))
    if document is not None:
        return document
    last_known = cache.get(_cache_key("discovery_last", idp))
    if cache.get(_cache_key("discovery_failed", idp)):
        return last_known
    try:
        response = requests.get(
            idp.discovery_url, verify=idp.verify_ssl, timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("discovery document is not a JSON object")
    except (requests.RequestException, ValueError) as e:
        logger.warning(
            "Unable to read the discovery document of identity provider %s: %s",
            idp.provider,
            e,
        )
        cache.set(_cache_key("discovery_failed", idp), True, FAILURE_CACHE_TIMEOUT)
        return last_known
    document = {name: body[name] for name in CACHED_KEYS if name in body}
    cache.set(_cache_key("discovery", idp), document, DISCOVERY_CACHE_TIMEOUT)
    cache.set(_cache_key("discovery_last", idp), document, LAST_KNOWN_CACHE_TIMEOUT)
    return document


def forget_discovery_document(idp: IdentityProvider) -> None:
    """Drop everything cached for the provider, so it is read afresh."""
    cache.delete_many(
        [
            _cache_key(kind, idp)
            for kind in (
                "discovery",
                "discovery_last",
                "discovery_failed",
                "token_auth_method",
            )
        ]
    )


def token_endpoint_auth_method(idp: IdentityProvider) -> str:
    """How the client credentials are sent to the provider's token endpoint.

    A method remembered after the provider rejected the other one comes first.
    Otherwise the request body is used unless the provider advertises HTTP
    Basic and not the body, which an omitted list does by default. Without any
    document the choice from before it was read applies: HTTP Basic for TARA,
    the body for everyone else.
    """
    remembered = cache.get(_cache_key("token_auth_method", idp))
    if remembered in FALLBACK_ORDER:
        return remembered
    document = get_discovery_document(idp)
    if document is None:
        if idp.provider == ProviderChoices.TARA:
            return TokenEndpointAuthMethod.CLIENT_SECRET_BASIC
        return TokenEndpointAuthMethod.CLIENT_SECRET_POST
    methods = document.get("token_endpoint_auth_methods_supported")
    if not isinstance(methods, list):
        methods = DEFAULT_AUTH_METHODS
    if (
        TokenEndpointAuthMethod.CLIENT_SECRET_BASIC in methods
        and TokenEndpointAuthMethod.CLIENT_SECRET_POST not in methods
    ):
        return TokenEndpointAuthMethod.CLIENT_SECRET_BASIC
    return TokenEndpointAuthMethod.CLIENT_SECRET_POST


def client_auth(
    idp: IdentityProvider, data: dict, method: str
) -> tuple[dict, tuple[str, str] | AuthBase | None]:
    """The request body and ``requests`` ``auth`` for *method*.

    With ``client_secret_basic`` the credentials go in the Authorization
    header, form-urlencoded first as RFC 6749 section 2.3.1 requires, and the
    body is left as it is; ``client_secret_basic_unencoded`` sends them in the
    header as they are; with ``client_secret_post`` both go in the body.
    """
    if method == TokenEndpointAuthMethod.CLIENT_SECRET_BASIC:
        return data, (quote_plus(idp.client_id), quote_plus(idp.client_secret))
    if method == CLIENT_SECRET_BASIC_UNENCODED:
        return data, _UnencodedBasicAuth(idp.client_id, idp.client_secret)
    return data | {"client_id": idp.client_id, "client_secret": idp.client_secret}, None


def _methods_to_try(idp: IdentityProvider, first: str) -> list[str]:
    """*first*, then the others in ``FALLBACK_ORDER``.

    Unencoded Basic is left out when form-urlencoding changes nothing.
    """
    methods = [first] + [m for m in FALLBACK_ORDER if m != first]
    if (
        quote_plus(idp.client_id) == idp.client_id
        and quote_plus(idp.client_secret) == idp.client_secret
    ):
        methods.remove(CLIENT_SECRET_BASIC_UNENCODED)
    return methods


def _client_rejected(response: requests.Response) -> bool:
    """Whether the provider refused the client authentication (RFC 6749 5.2)."""
    if response.status_code == 401:
        return True
    if response.status_code != 400:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("error") == "invalid_client"


def post_with_client_auth(
    idp: IdentityProvider, url: str, data: dict, timeout: float
) -> requests.Response:
    """POST *data* to one of the provider's token endpoints as the client.

    A provider rejecting the client authentication does so before it looks
    at the grant, so a code or refresh token is not used up by a rejected
    try, and the request is sent again with the other ways of authenticating
    until one is accepted. When none is, the answer to the first is returned.
    Raises ``requests.RequestException`` like ``requests.post``.
    """

    def post(method):
        body, auth = client_auth(idp, data, method)
        return requests.post(
            url, data=body, auth=auth, verify=idp.verify_ssl, timeout=timeout
        )

    first, *others = _methods_to_try(idp, token_endpoint_auth_method(idp))
    response = post(first)
    if not _client_rejected(response):
        return response
    for method in others:
        retry = post(method)
        if _client_rejected(retry):
            continue
        logger.info(
            "Identity provider %s rejected %s and accepted %s; using %s from now on.",
            idp.provider,
            first,
            method,
            method,
        )
        cache.set(
            _cache_key("token_auth_method", idp), method, REMEMBERED_METHOD_TIMEOUT
        )
        return retry
    return response
