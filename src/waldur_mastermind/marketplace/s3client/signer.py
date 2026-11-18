# MinIO Python Library for Amazon S3 Compatible Cloud Storage, (C)
# [2014] - [2025] MinIO, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Vendored from minio-py, minio/signer.py at commit 5d3f09ccb517
# (https://github.com/minio/minio-py). Changes by OpenNode LLC: the helpers it
# imported from minio.time, minio.checksum, minio.compat and minio.credentials
# are inlined below; headers are a plain dict instead of urllib3's
# HTTPHeaderDict; the HMAC helper is split into typed digest functions; the STS
# and POST-policy signers, which nothing here uses, are left out.

"""AWS Signature Version 4 for S3: signed headers and presigned URLs."""

from __future__ import annotations

import hashlib
import hmac
import re
import urllib.parse
from datetime import UTC, datetime
from typing import NamedTuple
from urllib.parse import SplitResult

SIGN_V4_ALGORITHM = "AWS4-HMAC-SHA256"
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"
_MULTI_SPACE_REGEX = re.compile(r"( +)")


class Credentials(NamedTuple):
    access_key: str
    secret_key: str


def sha256_hash(data: str | bytes | None) -> str:
    """SHA-256 of data, hex encoded."""
    data = data or b""
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def queryencode(value: str) -> str:
    """Encode a query parameter name or value the way SigV4 canonicalises it."""
    return urllib.parse.quote(value, safe="").replace("%7E", "~")


def _to_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def to_amz_date(value: datetime) -> str:
    return _to_utc(value).strftime("%Y%m%dT%H%M%SZ")


def to_signer_date(value: datetime) -> str:
    return _to_utc(value).strftime("%Y%m%d")


def _hmac_digest(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def _get_scope(date: datetime, region: str, service_name: str) -> str:
    return f"{to_signer_date(date)}/{region}/{service_name}/aws4_request"


def _get_canonical_headers(headers: dict[str, str]) -> tuple[str, str]:
    ordered: dict[str, list[str]] = {}
    for key, value in headers.items():
        key = key.lower()
        if key not in ("authorization", "user-agent"):
            ordered.setdefault(key, []).append(
                _MULTI_SPACE_REGEX.sub(" ", str(value)).strip()
            )
    items = sorted((key, ",".join(values)) for key, values in ordered.items())
    signed_headers = ";".join(key for key, _ in items)
    canonical_headers = "\n".join(f"{key}:{value}" for key, value in items)
    return canonical_headers, signed_headers


def _get_canonical_query_string(query: str) -> str:
    query = query or ""
    if not query:
        return ""
    # Each parameter canonicalises to "name=value"; a value-less one (such as
    # "acl") still takes a trailing "=". Sorted by the (name, value) pair.
    pairs = []
    for param in query.split("&"):
        name, _, value = param.partition("=")
        pairs.append((name, value))
    return "&".join(f"{name}={value}" for name, value in sorted(pairs))


def _get_canonical_request_hash(
    method: str,
    url: SplitResult,
    headers: dict[str, str],
    content_sha256: str,
) -> tuple[str, str]:
    canonical_headers, signed_headers = _get_canonical_headers(headers)
    canonical_query_string = _get_canonical_query_string(url.query)
    canonical_request = (
        f"{method}\n"
        f"{url.path or '/'}\n"
        f"{canonical_query_string}\n"
        f"{canonical_headers}\n\n"
        f"{signed_headers}\n"
        f"{content_sha256}"
    )
    return sha256_hash(canonical_request), signed_headers


def _get_string_to_sign(date: datetime, scope: str, canonical_request_hash: str) -> str:
    return (
        f"{SIGN_V4_ALGORITHM}\n{to_amz_date(date)}\n{scope}\n{canonical_request_hash}"
    )


def _get_signing_key(
    secret_key: str, date: datetime, region: str, service_name: str
) -> bytes:
    date_key = _hmac_digest(
        ("AWS4" + secret_key).encode(), to_signer_date(date).encode()
    )
    date_region_key = _hmac_digest(date_key, region.encode())
    date_region_service_key = _hmac_digest(date_region_key, service_name.encode())
    return _hmac_digest(date_region_service_key, b"aws4_request")


def _get_signature(signing_key: bytes, string_to_sign: str) -> str:
    return hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()


def sign_v4_s3(
    *,
    method: str,
    url: SplitResult,
    region: str,
    headers: dict[str, str],
    credentials: Credentials,
    content_sha256: str,
    date: datetime,
) -> dict[str, str]:
    """Sign a request for S3: sets the Authorization header in ``headers``.

    ``headers`` must already hold every header to sign, including Host,
    X-Amz-Date and X-Amz-Content-Sha256; ``url`` must be percent-encoded as
    sent.
    """
    scope = _get_scope(date, region, "s3")
    canonical_request_hash, signed_headers = _get_canonical_request_hash(
        method, url, headers, content_sha256
    )
    string_to_sign = _get_string_to_sign(date, scope, canonical_request_hash)
    signature = _get_signature(
        _get_signing_key(credentials.secret_key, date, region, "s3"), string_to_sign
    )
    headers["Authorization"] = (
        f"{SIGN_V4_ALGORITHM} Credential={credentials.access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return headers


def presign_v4(
    *,
    method: str,
    url: SplitResult,
    region: str,
    credentials: Credentials,
    date: datetime,
    expires: int,
) -> SplitResult:
    """A URL carrying its own SigV4 signature in the query, valid ``expires`` s."""
    scope = _get_scope(date, region, "s3")
    x_amz_credential = queryencode(credentials.access_key + "/" + scope)
    query = url.query + "&" if url.query else ""
    query += (
        f"X-Amz-Algorithm={SIGN_V4_ALGORITHM}"
        f"&X-Amz-Credential={x_amz_credential}"
        f"&X-Amz-Date={to_amz_date(date)}"
        f"&X-Amz-Expires={expires}"
        "&X-Amz-SignedHeaders=host"
    )
    url = url._replace(query=query)
    canonical_request = (
        f"{method}\n"
        f"{url.path or '/'}\n"
        f"{_get_canonical_query_string(query)}\n"
        f"host:{url.netloc}\n\n"
        "host\n"
        f"{UNSIGNED_PAYLOAD}"
    )
    string_to_sign = _get_string_to_sign(date, scope, sha256_hash(canonical_request))
    signature = _get_signature(
        _get_signing_key(credentials.secret_key, date, region, "s3"), string_to_sign
    )
    return url._replace(query=url.query + "&X-Amz-Signature=" + queryencode(signature))
