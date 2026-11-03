"""The cache of access tokens minted for users, encrypted.

Access tokens never reach the database: they stay in the cache, encrypted,
until shortly before they expire. The key carries a per-user generation, so
revoking a user's tokens drops every cached access token (any audience) at
once.
"""

import time

import jwt
from cryptography.fernet import InvalidToken
from django.core.cache import cache

from waldur_core.core import encryption

# Treat a token as expired a little early so it is still valid when the
# service checks it at the end of a slow request.
EXPIRY_LEEWAY_SECONDS = 30
# An opaque token carries no expiry to read; trust it this long.
OPAQUE_TOKEN_TTL_SECONDS = 60


def _generation_key(user_id):
    return f"oidc-access-token-generation:{user_id}"


def _cache_key(user_id, audience=None):
    generation = cache.get(_generation_key(user_id), 0)
    return f"oidc-access-token:{user_id}:{generation}:{audience or ''}"


def _seconds_left(access_token) -> int:
    try:
        claims = jwt.decode(access_token, options={"verify_signature": False})
    except jwt.PyJWTError:
        return OPAQUE_TOKEN_TTL_SECONDS
    expires_at = claims.get("exp")
    if expires_at is None:
        return OPAQUE_TOKEN_TTL_SECONDS
    return int(expires_at - EXPIRY_LEEWAY_SECONDS - time.time())


def cache_access_token(user, access_token, audience=None):
    if not access_token:
        return
    ttl = _seconds_left(access_token)
    if ttl > 0:
        cache.set(
            _cache_key(user.pk, audience), encryption.encrypt_value(access_token), ttl
        )


def cached_access_token(user, audience=None):
    key = _cache_key(user.pk, audience)
    value = cache.get(key)
    if not value:
        return None
    try:
        access_token = encryption.decrypt_value(value)
    except InvalidToken:
        cache.delete(key)
        return None
    return access_token if _seconds_left(access_token) > 0 else None


def forget_access_tokens(user_id):
    key = _generation_key(user_id)
    cache.set(key, cache.get(key, 0) + 1, None)
