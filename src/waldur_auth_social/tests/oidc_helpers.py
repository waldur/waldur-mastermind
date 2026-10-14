"""
A fake OIDC signing setup for tests: an RSA key, its JWKS, and ID tokens signed with it.

Discovery and JWKS endpoints are registered with `responses`, so a test using
these helpers must have `responses` started.
"""

import json
import time
from urllib.parse import urlparse

import jwt
import responses
from cryptography.hazmat.primitives.asymmetric import rsa
from rest_framework import status

SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
SIGNING_KEY_ID = "test-key"


def public_jwk(private_key=SIGNING_KEY, kid=SIGNING_KEY_ID):
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return jwk


def issuer_of(idp):
    parsed = urlparse(idp.discovery_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def jwks_url_of(idp):
    return f"{issuer_of(idp)}/jwks"


def mock_signing_keys(idp, keys=None):
    """Register the provider's discovery document and JWKS endpoint."""
    responses.add(
        method="GET",
        url=idp.discovery_url,
        json={"issuer": issuer_of(idp), "jwks_uri": jwks_url_of(idp)},
        status=status.HTTP_200_OK,
    )
    return responses.add(
        method="GET",
        url=jwks_url_of(idp),
        json={"keys": keys if keys is not None else [public_jwk()]},
        status=status.HTTP_200_OK,
    )


def make_id_token(
    idp, sub, private_key=SIGNING_KEY, kid=SIGNING_KEY_ID, algorithm="RS256", **claims
):
    now = int(time.time())
    payload = {
        "iss": issuer_of(idp),
        "aud": idp.client_id,
        "sub": sub,
        "iat": now,
        "exp": now + 40,
        "acr": "high",
    }
    payload.update(claims)
    payload = {key: value for key, value in payload.items() if value is not None}
    return jwt.encode(payload, private_key, algorithm=algorithm, headers={"kid": kid})


def mock_token_endpoint(
    idp, get_sub, access_token="test_access_token", refresh_token="test_refresh_token"
):
    """
    Register the token endpoint returning an ID token signed for the provider.

    `get_sub` is called when the request is made, so the subject can follow the
    user info a test registers after the token mock.
    """
    mock_signing_keys(idp)

    def callback(request):
        body = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": make_id_token(idp, get_sub()),
        }
        return status.HTTP_200_OK, {}, json.dumps(body)

    return responses.add(
        responses.CallbackResponse(
            method="POST",
            url=idp.token_url,
            callback=callback,
            content_type="application/json",
        )
    )
