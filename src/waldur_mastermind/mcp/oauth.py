"""The OAuth authorization server for MCP clients, built on django-oauth-toolkit."""

import functools
import unicodedata
from dataclasses import dataclass
from types import SimpleNamespace
from urllib.parse import urlencode, urlparse

from django.conf import settings
from oauth2_provider.models import (
    AbstractApplication,
    get_access_token_model,
    get_grant_model,
    get_refresh_token_model,
)
from oauth2_provider.oauth2_backends import (
    OAuthLibCore,
    _add_iss_to_redirect,
    get_oauthlib_core,
)
from oauth2_provider.oauth2_validators import (
    OAuth2Validator,
    _parse_and_validate_uri,
)
from oauth2_provider.settings import oauth2_settings
from oauthlib.common import Request
from oauthlib.oauth2.rfc6749 import errors
from rest_framework.throttling import SimpleRateThrottle

from waldur_core.configuration import config

# The only grants that act for a user who consented. client_credentials would
# give any registered client a token without anyone's consent.
ALLOWED_GRANT_TYPES = ("authorization_code", "refresh_token")


def get_resource_urls() -> list[str]:
    return config.MCP_RESOURCE_URLS.split()


def parse_resource(uri: str) -> tuple | None:
    # DOT's own parser, so issuance and DOT's audience check at use time agree
    # on which spellings are the same resource. It lets urlsplit's ValueError
    # out for a URI such as http://[foo.
    try:
        return _parse_and_validate_uri(uri)
    except ValueError:
        return None


def get_allowed_resources() -> set[tuple]:
    parsed = (parse_resource(url) for url in get_resource_urls())
    return {resource for resource in parsed if resource is not None}


def is_allowed_resource(uri: str, allowed: set[tuple] | None = None) -> bool:
    if allowed is None:
        allowed = get_allowed_resources()
    resource = parse_resource(uri)
    return resource is not None and resource in allowed


class ClientRegistrationThrottle(SimpleRateThrottle):
    """New OAuth clients per client address, registered by DCR or by CIMD alike."""

    scope = "mcp_client_registration"

    def get_ident(self, request):
        # DRF's own get_ident uses the whole X-Forwarded-For while NUM_PROXIES is
        # unset, and the client writes that header. Count from the right instead:
        # those entries were added by the deployment's proxies.
        proxies = settings.MCP_OAUTH_TRUSTED_PROXY_COUNT
        forwarded = [
            address.strip()
            for address in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")
            if address.strip()
        ]
        if proxies and forwarded:
            return forwarded[-min(proxies, len(forwarded))]
        return request.META.get("REMOTE_ADDR")

    def get_cache_key(self, request, view):
        return self.cache_format % {
            "scope": self.scope,
            "ident": self.get_ident(request),
        }


class ThrottledCIMDPermission:
    """Limits how often unknown CIMD URLs are fetched and saved as clients.

    DOT asks this before every fetch, so clients it already knows don't count.
    """

    def has_permission(self, request, client_id) -> bool:
        # DOT passes the oauthlib request, whose headers are a copy of Django's META.
        client = SimpleNamespace(META=dict(request.headers))
        return ClientRegistrationThrottle().allow_request(client, None)


def code_gone_is_invalid_grant(check):
    """For DOT's checks that read the code again after validate_code, and let a
    code deleted meanwhile, e.g. by a revoke or a second exchange, out as a 500."""

    @functools.wraps(check)
    def wrapper(*args, **kwargs):
        try:
            return check(*args, **kwargs)
        except get_grant_model().DoesNotExist:
            raise errors.InvalidGrantError()

    return wrapper


class MCPOAuth2Validator(OAuth2Validator):
    get_code_challenge = code_gone_is_invalid_grant(OAuth2Validator.get_code_challenge)
    get_code_challenge_method = code_gone_is_invalid_grant(
        OAuth2Validator.get_code_challenge_method
    )
    confirm_redirect_uri = code_gone_is_invalid_grant(
        OAuth2Validator.confirm_redirect_uri
    )

    def validate_redirect_uri(self, client_id, redirect_uri, request, *args, **kwargs):
        # DOT reads the URI's port, which raises for one such as :99999.
        try:
            return super().validate_redirect_uri(
                client_id, redirect_uri, request, *args, **kwargs
            )
        except ValueError:
            return False

    def validate_grant_type(
        self, client_id, grant_type, client, request, *args, **kwargs
    ):
        if grant_type not in ALLOWED_GRANT_TYPES:
            return False
        return super().validate_grant_type(
            client_id, grant_type, client, request, *args, **kwargs
        )

    def _load_application(self, client_id, request):
        # PostgreSQL can't look up a null byte, and DOT would let its error out.
        if client_id and "\x00" in client_id:
            return None
        return super()._load_application(client_id, request)

    def validate_code(self, client_id, code, client, request, *args, **kwargs):
        if "\x00" in code:
            return False
        # DOT doesn't check is_active, so a deactivated user's pending code or
        # refresh token would keep minting access tokens.
        valid = super().validate_code(client_id, code, client, request, *args, **kwargs)
        return valid and request.user.is_active

    def validate_refresh_token(self, refresh_token, client, request, *args, **kwargs):
        valid = super().validate_refresh_token(
            refresh_token, client, request, *args, **kwargs
        )
        return valid and request.user is not None and request.user.is_active

    def save_bearer_token(self, token, request, *args, **kwargs):
        try:
            return super().save_bearer_token(token, request, *args, **kwargs)
        except (
            get_refresh_token_model().DoesNotExist,
            get_access_token_model().DoesNotExist,
        ):
            # Deleted after it was checked, e.g. by a revoke; DOT doesn't catch it.
            # Without rotation, DOT reuses the refresh token's access token, so
            # that is the one found missing.
            raise errors.InvalidGrantError(request=request)

    def _save_bearer_token(self, token, request, *args, **kwargs):
        # DOT checks the code without a lock and deletes it only after saving
        # the tokens. Locked, a revoke, which deletes codes first, either waits
        # and then deletes these tokens too, or has deleted the code already.
        if request.grant_type == "authorization_code" and not (
            get_grant_model()
            .objects.select_for_update()
            .filter(code=request.code, application=request.client)
            .exists()
        ):
            raise errors.InvalidGrantError(request=request)
        return super()._save_bearer_token(token, request, *args, **kwargs)

    def _validate_resource_uris(self, request, resources):
        # Also runs on resources inherited from the grant or refresh token, so a
        # resource dropped from MCP_RESOURCE_URLS stops being refreshable. DOT's
        # own check adds nothing to this one, and can raise on a malformed URI.
        allowed = get_allowed_resources()
        for resource in resources:
            if not is_allowed_resource(resource, allowed):
                # Not echoing the value: the consent page would show the
                # attacker's text under Waldur's name.
                raise errors.CustomOAuth2Error(
                    error="invalid_target",
                    description="The requested resource is not served here.",
                    request=request,
                )

    def _check_and_set_request_resource(self, request):
        super()._check_and_set_request_resource(request)
        if request.resource:
            return
        # A client that names no resource still gets a token bound to the MCP
        # endpoint, never one that is valid everywhere. Only the first URL, so
        # dropping another one later doesn't break these sessions' refresh.
        request.resource = self._get_default_resources(request)

    def _get_default_resources(self, request):
        resources = get_resource_urls()[:1]
        if not resources:
            raise errors.CustomOAuth2Error(
                error="invalid_target",
                description="No MCP resource is configured.",
                request=request,
            )
        # A malformed setting fails here rather than yielding a token that can
        # never be used or refreshed.
        self._validate_resource_uris(request, resources)
        return resources

    def validate_requested_resources(self, request, resources) -> list[str]:
        """Refuses at consent what the token endpoint would refuse after it.

        Returns the resources the token will be bound to.
        """
        if resources:
            self._validate_resource_uris(request, resources)
            return resources
        return self._get_default_resources(request)


@dataclass
class AuthorizationRequest:
    core: OAuthLibCore
    client: AbstractApplication
    oauth_request: Request
    scopes: list[str]
    # What create_authorization_response needs to issue the code.
    credentials: dict
    # Those requested, or the default the grant gets when none are.
    resources: list[str]

    @property
    def redirect_host(self) -> str:
        return urlparse(self.credentials["redirect_uri"]).hostname

    @property
    def client_name(self) -> str:
        # Registration doesn't require a name, but the user must see who asks.
        return displayable(self.client.name) or self.redirect_host


def displayable(text: str) -> str:
    """The client's own text, without what could make it read as something else.

    Line breaks become spaces, and control and format characters such as a
    right-to-left override are dropped.
    """
    kept = "".join(
        char
        for char in text
        if char.isspace() or not unicodedata.category(char).startswith("C")
    )
    return " ".join(kept.split())


# Characters Python's IDNA 2003 codec maps away, but browsers (UTS 46) keep or
# refuse, so the codec would name another host than the browser looks up.
IDNA_DEVIATIONS = frozenset("\u00df\u03c2\u200c\u200d")


def displayable_host(host: str | None) -> str | None:
    """The host in ASCII, as a browser looks it up, so a lookalike such as
    claude.ai spelled with a Cyrillic "c" shows as xn--laude-0ye.ai.
    """
    if not host:
        return host
    if not host.isascii() and not IDNA_DEVIATIONS.intersection(host):
        try:
            return host.encode("idna").decode()
        except UnicodeError:
            pass
    # Escaped rather than tidied, so it can't pass for another host either.
    return host.encode("unicode_escape").decode()


def get_hostname(uri: str) -> str | None:
    # Registration checks the URIs with urlparse too, but a newer Python may
    # refuse one a stored client has, and one client mustn't break the list.
    try:
        return urlparse(uri).hostname
    except ValueError:
        return None


def get_client_host(client: AbstractApplication) -> str | None:
    # Only a CIMD client_id is a URL. DOT doesn't keep a DCR client's URI.
    if client.registration_source == client.RegistrationSource.CIMD:
        return displayable_host(get_hostname(client.client_id))
    return None


def get_redirect_hosts(client: AbstractApplication) -> list[str]:
    """The hosts of all the client's redirect URIs, in registration order.

    All of them, as DOT deletes the code, and with it the URI it went to, once
    the code is exchanged.
    """
    hosts = (get_hostname(uri) for uri in client.redirect_uris.split())
    return list(dict.fromkeys(displayable_host(host) for host in hosts if host))


def get_client_name(client: AbstractApplication) -> str:
    """The name to show for a client outside an authorization request."""
    return (
        displayable(client.name)
        or get_client_host(client)
        or next(iter(get_redirect_hosts(client)), None)
        or client.client_id
    )


def validate_authorization_request(request, params) -> AuthorizationRequest:
    """Checks an authorization request, raising oauthlib's error if it is invalid.

    Takes the parameters separately because the consent API receives them in a
    JSON body, and oauthlib reads them from the URI. On top of oauthlib, it makes
    the checks that DOT only makes after the user approved, or not at all.
    """
    core = get_oauthlib_core()
    scopes, info = core.server.validate_authorization_request(
        "/oauth/authorize/?" + urlencode(params, doseq=True),
        http_method="GET",
        headers=core.extract_headers(request),
    )
    oauth_request = info["request"]
    client = oauth_request.client

    # A consent API response skips the check DOT makes when it redirects itself.
    if urlparse(info["redirect_uri"]).scheme not in client.get_allowed_schemes():
        raise errors.InvalidRedirectURIError(request=oauth_request)

    # oauthlib treats a missing method as plain.
    if info.get("code_challenge_method") != "S256":
        raise errors.InvalidRequestError(
            description="The code_challenge_method must be S256.",
            request=oauth_request,
        )

    resources = params.get("resource", [])
    bound_resources = core.server.request_validator.validate_requested_resources(
        oauth_request, resources
    )

    credentials = {
        "client_id": info["client_id"],
        "redirect_uri": info["redirect_uri"],
        "response_type": info["response_type"],
        "state": info["state"],
        "code_challenge": info["code_challenge"],
        "code_challenge_method": info["code_challenge_method"],
    }
    if resources:
        credentials["resource"] = resources
    return AuthorizationRequest(
        core, client, oauth_request, scopes, credentials, bound_resources
    )


def add_issuer(request, url: str) -> str:
    """Adds RFC 9207 iss, which DOT only adds to the redirect that carries a code."""
    return _add_iss_to_redirect(
        url, oauth2_settings.oauth2_authorization_server_issuer(request)
    )
