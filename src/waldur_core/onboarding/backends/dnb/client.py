"""
Shared Dun & Bradstreet (Bisnode) API client.

Two endpoints are wired up here:

- Credit Data Companies API (/credit-data-companies/v2/companies/{country}),
  scope `credit_data_companies` — used by the NO/DK/FI backends.
- Nordic Right to Sign API (/nordic-rts/v1/rts/company-signatories/detailed/{COUNTRY}),
  scope `nordic_right_to_sign` — used by the SE backend.

Both use the same OAuth2 client_credentials token endpoint, but issued
tokens are scope-specific, so each scope needs its own client instance
(and therefore its own cached token).
"""

import logging
import threading
import time
from typing import Any
from urllib.parse import urlparse

import requests
from constance import config

logger = logging.getLogger(__name__)

_TOKEN_REFRESH_SAFETY_SECONDS = 60
_HTTP_TIMEOUT = (5, 30)  # (connect, read) seconds


class DnbError(Exception):
    """Raised for any D&B API failure. `not_found=True` signals a 404."""

    def __init__(self, message: str, not_found: bool = False):
        super().__init__(message)
        self.not_found = not_found


class DnbClient:
    """
    Shared client for the D&B Credit Data Companies API.

    Single instance per process (see get_dnb_client). Caches the OAuth2
    token with expiry tracking.
    """

    def __init__(
        self,
        api_url: str,
        token_url: str,
        client_id: str,
        client_secret: str,
        scope: str = "credit_data_companies",
    ):
        self.api_url = api_url.rstrip("/")
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.scope = scope
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        # Serializes token refresh so N concurrent callers don't all hit
        # the token endpoint when the cached token expires.
        self._token_lock = threading.Lock()

    def _fresh_token(self) -> str | None:
        if (
            self._token
            and time.time() < self._token_expires_at - _TOKEN_REFRESH_SAFETY_SECONDS
        ):
            return self._token
        return None

    def _get_token(self, force_refresh: bool = False) -> str:
        # Fast path: a valid cached token needs no locking.
        if not force_refresh and (token := self._fresh_token()):
            return token

        with self._token_lock:
            # Another thread may have refreshed while we waited for the lock.
            if not force_refresh and (token := self._fresh_token()):
                return token

            if not self.client_id or not self.client_secret:
                raise ValueError(
                    "Dun & Bradstreet client credentials are not configured"
                )

            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            body = {
                "grant_type": "client_credentials",
                "scope": self.scope,
            }

            # Bisnode's token endpoint requires client_secret_basic — sending
            # credentials in the form body (client_secret_post) returns 401.
            try:
                response = requests.post(
                    self.token_url,
                    headers=headers,
                    data=body,
                    auth=(self.client_id, self.client_secret),
                    timeout=_HTTP_TIMEOUT,
                )
                response.raise_for_status()
                data = response.json()
            except requests.exceptions.HTTPError as e:
                detail = self._token_error_detail(e.response)
                suffix = f" ({detail})" if detail else ""
                raise DnbError(f"Access token request failed: {e}{suffix}")
            except requests.exceptions.RequestException as e:
                raise DnbError(f"Access token request failed: {e}")

            access_token = data.get("access_token")
            if not access_token:
                raise DnbError("Access token missing in D&B token response")

            self._token = access_token
            self._token_expires_at = time.time() + int(data.get("expires_in", 3600))
            return access_token

    def get_company(
        self,
        country: str,
        registration_number: str,
        segments: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Fetch company credit data for the given country/registration number.

        country: ISO country code lowercased (se, no, dk, fi).
        segments: optional list of data segments (e.g. ["MANAGEMENT"]).
        """
        path = f"/companies/{country.lower()}"
        url = f"{self.api_url}{path}"
        payload: dict[str, Any] = {"registrationNumber": registration_number}
        if segments:
            payload["segments"] = segments

        return self._post_with_retry(url, payload)

    def check_rts(
        self,
        country: str,
        registration_number: str,
        signatories: list[dict[str, Any]],
        sign_together: str = "ANY",
        authority_type: str | None = "DEFAULT",
    ) -> dict[str, Any]:
        """
        Call Nordic Right to Sign /rts/company-signatories/detailed/{COUNTRY}.

        country: ISO country code uppercased on the wire (the path uses SE/NO/DK/FI).
        signatories: list of {ssn} (SE) or {name:{firstName,lastName}, birthDate}
            (NO). Per the spec at least one strong identifier must be set per
            item. NB: DK does NOT use this endpoint — its company-signatories
            variant requires an id or name+address per signatory, so DK goes
            through ``check_company_rts`` instead and matches the person locally.
        sign_together: ANY | AT_LEAST | AT_MOST | EXACTLY. Defaults to ANY,
            which is the right choice for "is this single user authorized?"
        authority_type: DEFAULT | PROCURATION, or ``None`` to omit the field.
            DEFAULT covers the regular signing rights (board members, MD, etc.).
            PROCURATION returns a separate rule set for delegated procuration
            holders; the doc table shows the same orgnr may have very different
            results between the two. Required by the NO endpoint.
        """
        path = f"/rts/company-signatories/detailed/{country.upper()}"
        url = f"{self.api_url}{path}"
        payload: dict[str, Any] = {
            "registrationNumber": registration_number,
            "signatories": signatories,
            "signTogether": sign_together,
        }
        if authority_type is not None:
            payload["authorityType"] = authority_type
        return self._post_with_retry(url, payload)

    def check_company_rts(
        self,
        country: str,
        registration_number: str,
        authority_type: str | None = "DEFAULT",
    ) -> dict[str, Any]:
        """
        Call Nordic Right to Sign /rts/company/detailed/{COUNTRY}.

        Company-level lookup: given only the registration number, returns the
        company's full signing structure — ``signatories[]`` / ``coSignatories[]``
        / ``nonSignatories[]`` (each with ``name``, ``cvrId``, ``roles``),
        ``signingRules`` and company info. Unlike ``check_rts`` it does NOT take
        a per-person identifier, so it sidesteps DK's "id or name+address
        required" constraint: the caller matches the target person against the
        returned lists locally. Response shape matches ``check_rts`` so the same
        normalizers/matchers apply.

        authority_type: DEFAULT | PROCURATION, or ``None`` to omit the field.
        """
        path = f"/rts/company/detailed/{country.upper()}"
        url = f"{self.api_url}{path}"
        payload: dict[str, Any] = {"registrationNumber": registration_number}
        if authority_type is not None:
            payload["authorityType"] = authority_type
        return self._post_with_retry(url, payload)

    def _post_with_retry(self, url: str, payload: dict) -> dict[str, Any]:
        response = self._do_post(url, payload, token=self._get_token())
        if response.status_code == 401:
            # Token may be stale despite expiry math; force refresh + retry once.
            response = self._do_post(
                url, payload, token=self._get_token(force_refresh=True)
            )
            if response.status_code == 401:
                raise DnbError(
                    "D&B API authentication failed after token refresh — "
                    "check client credentials"
                )

        if response.status_code == 404:
            raise DnbError(
                f"D&B company not found at {url}",
                not_found=True,
            )

        try:
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            detail = self._safe_detail(response)
            # Log the full body server-side: D&B's generic
            # "Request body contains one or more errors" message doesn't tell
            # operators which field was rejected. The raw payload usually does.
            text_body = getattr(response, "text", "") or ""
            if not isinstance(text_body, str):
                text_body = repr(text_body)
            logger.warning(
                "D&B API %s on %s — body: %s",
                response.status_code,
                url,
                text_body[:2000],
            )
            raise DnbError(f"D&B API error ({response.status_code}): {detail or e}")

        try:
            return response.json()
        except ValueError as e:
            raise DnbError(f"D&B API returned non-JSON body: {e}")

    def _do_post(self, url: str, payload: dict, token: str):
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        try:
            return requests.post(
                url, headers=headers, json=payload, timeout=_HTTP_TIMEOUT
            )
        except requests.exceptions.RequestException as e:
            raise DnbError(f"D&B API request failed: {e}")

    @staticmethod
    def _safe_detail(response) -> str:
        # D&B's error envelopes vary by endpoint. Credit Data tends to use
        # {detail}/{error}; Nordic RTS tends to use {message} or
        # {message, validationErrors:[{field, message}]}. Fall back to the
        # raw body so operators get *something* when a new shape appears
        # rather than a bare "400 Client Error".
        try:
            body = response.json()
        except (ValueError, AttributeError):
            text = getattr(response, "text", "") or ""
            return text.strip()[:500]
        if not isinstance(body, dict):
            return ""

        base = (
            body.get("detail")
            or body.get("error")
            or body.get("message")
            or body.get("errorMessage")
            or ""
        )

        # Bisnode RTS validation errors arrive in a list under any of several
        # keys; format each entry compactly so the per-field reason surfaces
        # in the wrapped DnbError.
        parts: list[str] = []
        for list_key in ("validationErrors", "errors", "details", "fieldErrors"):
            for err in body.get(list_key) or []:
                if not isinstance(err, dict):
                    continue
                field = (
                    err.get("field")
                    or err.get("path")
                    or err.get("location")
                    or err.get("name")
                    or ""
                )
                message = (
                    err.get("message")
                    or err.get("detail")
                    or err.get("description")
                    or err.get("reason")
                    or ""
                )
                code = err.get("code") or err.get("errorCode") or ""
                pieces = [p for p in (field, message, code) if p]
                if pieces:
                    parts.append(": ".join(pieces))

        # Surface a top-level "code" if D&B sent one without a list — useful
        # when the message is generic ("Request body contains one or more
        # errors") but the code distinguishes VALIDATION_ERROR vs BAD_REQUEST.
        top_code = body.get("code") or body.get("errorCode") or ""
        if top_code:
            parts.append(f"code={top_code}")

        if parts:
            extra = "; ".join(parts)
            return f"{base} ({extra})" if base else extra
        return base

    @staticmethod
    def _token_error_detail(response) -> str:
        # OAuth2 token errors use {"error": "...", "error_description": "..."}
        # (RFC 6749 §5.2). Surfacing both lets operators distinguish
        # invalid_client vs invalid_scope vs unauthorized_client without
        # digging into server logs.
        if response is None:
            return ""
        try:
            body = response.json()
        except (ValueError, AttributeError):
            return ""
        if not isinstance(body, dict):
            return ""
        error = body.get("error") or ""
        description = body.get("error_description") or ""
        if error and description:
            return f"{error}: {description}"
        return error or description


def _validate_endpoint_url(url: str, label: str) -> None:
    """
    Reject endpoint URLs that aren't HTTPS with a real host.

    The token endpoint receives the OAuth ``client_secret`` (HTTP Basic) and
    the API endpoints receive the bearer token, so a Constance value pointing
    at ``http://``, a hostless string, or other garbage would leak credentials
    in cleartext or enable SSRF to an arbitrary host. Constance is admin-
    writable, so this guard is what stops a bad config value from exfiltrating
    secrets to an attacker-controlled or internal address.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(
            f"Dun & Bradstreet {label} must be an absolute https:// URL "
            "with a valid host"
        )


_client: DnbClient | None = None
# Serializes (re)construction of the module-level singleton so threaded
# workers don't race to build duplicate clients on first use or after a
# credential rotation.
_client_lock = threading.Lock()


def get_dnb_client() -> DnbClient:
    """
    Module-level singleton. Re-reads Constance on every call so admins
    can rotate credentials at runtime; only rebuilds the client (and
    discards the cached token) when a config value actually changed.
    """
    global _client
    api_url = config.ONBOARDING_DNB_API_URL
    token_url = config.ONBOARDING_DNB_TOKEN_URL
    client_id = config.ONBOARDING_DNB_CLIENT_ID
    client_secret = config.ONBOARDING_DNB_CLIENT_SECRET

    if not api_url:
        raise ValueError("Dun & Bradstreet API URL is not configured")
    if not token_url:
        raise ValueError("Dun & Bradstreet token URL is not configured")
    _validate_endpoint_url(api_url, "API URL")
    _validate_endpoint_url(token_url, "token URL")

    normalized_api_url = api_url.rstrip("/")
    with _client_lock:
        if (
            _client is None
            or _client.api_url != normalized_api_url
            or _client.token_url != token_url
            or _client.client_id != client_id
            or _client.client_secret != client_secret
        ):
            _client = DnbClient(
                api_url=api_url,
                token_url=token_url,
                client_id=client_id,
                client_secret=client_secret,
            )
        return _client


def reset_dnb_client() -> None:
    """Test hook: clear the cached client so Constance changes take effect."""
    global _client
    with _client_lock:
        _client = None


_rts_client: DnbClient | None = None
_rts_client_lock = threading.Lock()


def get_dnb_rts_client() -> DnbClient:
    """
    Singleton client for the Nordic Right to Sign API.

    Uses the same OAuth2 token endpoint and credentials as the credit-data
    client, but a different scope (`nordic_right_to_sign`) and base URL —
    so its token cache is separate from `get_dnb_client()`'s.
    """
    global _rts_client
    api_url = config.ONBOARDING_DNB_RTS_API_URL
    token_url = config.ONBOARDING_DNB_TOKEN_URL
    client_id = config.ONBOARDING_DNB_CLIENT_ID
    client_secret = config.ONBOARDING_DNB_CLIENT_SECRET

    if not api_url:
        raise ValueError("Dun & Bradstreet RTS API URL is not configured")
    if not token_url:
        raise ValueError("Dun & Bradstreet token URL is not configured")
    _validate_endpoint_url(api_url, "RTS API URL")
    _validate_endpoint_url(token_url, "token URL")

    normalized_api_url = api_url.rstrip("/")
    with _rts_client_lock:
        if (
            _rts_client is None
            or _rts_client.api_url != normalized_api_url
            or _rts_client.token_url != token_url
            or _rts_client.client_id != client_id
            or _rts_client.client_secret != client_secret
        ):
            _rts_client = DnbClient(
                api_url=api_url,
                token_url=token_url,
                client_id=client_id,
                client_secret=client_secret,
                scope="nordic_right_to_sign",
            )
        return _rts_client


def reset_dnb_rts_client() -> None:
    """Test hook: clear the cached RTS client so Constance changes take effect."""
    global _rts_client
    with _rts_client_lock:
        _rts_client = None
