"""
Validation of the OIDC ID token returned by the identity provider's token endpoint.

Signing keys are always loaded from the provider's JWKS endpoint and are never
configured by hand, so a key rotation on the provider side is picked up without
any admin action: the key set is cached, and a token signed with a key id that
is not in the cached set triggers a fresh fetch.
"""

import hashlib
import logging

import jwt
import requests
from django.core.cache import cache

from waldur_auth_social.const import ProviderChoices
from waldur_auth_social.exceptions import OAuthException

logger = logging.getLogger(__name__)

# TARA recommends caching the key set for 5 minutes to 24 hours.
JWKS_CACHE_TIMEOUT = 60 * 60

# Allowed clock difference between Waldur and the identity provider, in seconds.
CLOCK_LEEWAY = 30

# Asymmetric algorithms only: "none" and the HMAC family are never accepted.
ALLOWED_ALGORITHMS = [
    "RS256",
    "RS384",
    "RS512",
    "PS256",
    "PS384",
    "PS512",
    "ES256",
    "ES384",
    "ES512",
    "EdDSA",
]

# Minimum level of assurance accepted from TARA.
TARA_ACCEPTED_ACR = ("substantial", "high")

REQUEST_TIMEOUT = 10


def ensure_discovery_metadata(idp):
    """Fill issuer and jwks_url of a provider created before they were stored."""
    if idp.issuer and idp.jwks_url:
        return
    try:
        response = requests.get(
            idp.discovery_url, verify=idp.verify_ssl, timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        document = response.json()
        issuer = document["issuer"]
        jwks_url = document["jwks_uri"]
    except (requests.RequestException, ValueError, KeyError, TypeError) as e:
        logger.warning(
            "Unable to read issuer and JWKS URL of identity provider %s: %s",
            idp.provider,
            e,
        )
        raise OAuthException(
            idp.provider, "Unable to discover identity provider signing keys."
        )
    idp.issuer = issuer
    idp.jwks_url = jwks_url
    idp.save(update_fields=["issuer", "jwks_url"])


def jwks_cache_key(idp):
    digest = hashlib.sha256(idp.jwks_url.encode()).hexdigest()
    return f"oidc_jwks:{idp.provider}:{digest}"


def _fetch_jwks(idp):
    try:
        response = requests.get(
            idp.jwks_url, verify=idp.verify_ssl, timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        jwks = response.json()
        jwt.PyJWKSet.from_dict(jwks)
    except (requests.RequestException, ValueError, jwt.PyJWTError) as e:
        logger.warning(
            "Unable to fetch signing keys of identity provider %s: %s",
            idp.provider,
            e,
        )
        raise OAuthException(
            idp.provider, "Unable to fetch identity provider signing keys."
        )
    cache.set(jwks_cache_key(idp), jwks, timeout=JWKS_CACHE_TIMEOUT)
    return jwks


def _find_key(jwks, kid):
    try:
        key_set = jwt.PyJWKSet.from_dict(jwks)
    except jwt.PyJWTError:
        return None
    for key in key_set.keys:
        if key.key_id == kid:
            return key
    return None


def get_signing_key(idp, kid):
    jwks = cache.get(jwks_cache_key(idp))
    key = _find_key(jwks, kid) if jwks else None
    if key is None:
        # Unknown key id: the provider may have rotated its key since the set
        # was cached, so fetch it again before giving up.
        key = _find_key(_fetch_jwks(idp), kid)
    if key is None:
        logger.warning(
            "ID token of identity provider %s is signed with unknown key %r.",
            idp.provider,
            kid,
        )
        raise OAuthException(idp.provider, "Invalid ID token signature.")
    return key


def validate_id_token(idp, raw_token):
    """Verify signature and claims of an ID token and return its claims."""
    if not raw_token:
        raise OAuthException(
            idp.provider, "Authentication response does not contain ID token."
        )
    ensure_discovery_metadata(idp)

    try:
        header = jwt.get_unverified_header(raw_token)
    except jwt.PyJWTError as e:
        logger.warning("Malformed ID token from %s: %s", idp.provider, e)
        raise OAuthException(idp.provider, "Invalid ID token.")

    if header.get("alg") not in ALLOWED_ALGORITHMS:
        logger.warning(
            "ID token of identity provider %s uses disallowed algorithm %r.",
            idp.provider,
            header.get("alg"),
        )
        raise OAuthException(idp.provider, "Invalid ID token signature.")

    key = get_signing_key(idp, header.get("kid"))

    try:
        claims = jwt.decode(
            raw_token,
            key=key,
            algorithms=ALLOWED_ALGORITHMS,
            audience=idp.client_id,
            issuer=idp.issuer,
            leeway=CLOCK_LEEWAY,
            options={"require": ["iss", "aud", "exp", "iat", "sub"]},
        )
    except jwt.PyJWTError as e:
        logger.warning("ID token of %s is rejected: %s", idp.provider, e)
        raise OAuthException(idp.provider, "Invalid ID token.")

    if (
        idp.provider == ProviderChoices.TARA
        and claims.get("acr") not in TARA_ACCEPTED_ACR
    ):
        logger.warning(
            "ID token of %s has insufficient level of assurance %r.",
            idp.provider,
            claims.get("acr"),
        )
        raise OAuthException(idp.provider, "Insufficient level of assurance.")

    return claims
