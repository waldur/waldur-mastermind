"""Browse and manage an S3 resource's buckets through one of its API keys.

Mastermind signs every S3 call itself with the key's stored secret, so the
browser never holds the secret and no consumer bucket is changed to allow it
(no CORS rules, no policies). Downloads are handed out as short-lived presigned
URLs that the browser fetches from the gateway directly; uploads are small and
proxied, with a hard size cap.

The gateway address comes from the resource's backend metadata, which the
provider's site agent writes. That makes every call here an outbound request to
a provider-chosen host, so only hosts staff have allowlisted are contacted.

Gateways implement different parts of S3. The offering names its gateway's
flavor, and the browser reads, and shows, only what that flavor supports.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import IO
from urllib.parse import quote, urlsplit

from cryptography.fernet import InvalidToken
from django.core.cache import cache
from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions as rf_exceptions
from rest_framework import status

from waldur_core.configuration import config
from waldur_core.core import encryption
from waldur_core.core.exceptions import IncorrectStateException

from . import models
from .enums import S3BrowserFlavors
from .s3client import S3Client, S3ClientError, S3ConnectionError, S3TimeoutError

logger = logging.getLogger(__name__)

PLUGIN_OPTION = "enable_s3_browser"
FLAVOR_OPTION = "s3_browser_flavor"

# The configuration reads behind bucket and object details, by response field.
BUCKET_PARTS = (
    "location",
    "versioning",
    "object_lock",
    "acl",
    "policy",
    "policy_public",
    "public_access_block",
    "lifecycle_rules",
    "encryption",
    "cors_rules",
    "tags",
)
OBJECT_PARTS = ("acl", "tags", "versions")


@dataclass(frozen=True)
class Flavor:
    """What one kind of S3 gateway implements, as far as the browser cares."""

    # Signing region when the resource publishes none.
    default_region: str
    # Object count and bytes used per bucket, from RGW's X-RGW-* headers.
    bucket_stats: bool
    # The bucket and object configuration reads the gateway implements; a read
    # outside them is never sent. A generic gateway is asked for every one, and
    # those it refuses come back listed as unavailable.
    bucket_parts: tuple[str, ...] = BUCKET_PARTS
    object_parts: tuple[str, ...] = OBJECT_PARTS


GENERIC = S3BrowserFlavors.GENERIC
FLAVORS = {
    GENERIC: Flavor(default_region="us-east-1", bucket_stats=False),
    S3BrowserFlavors.CEPH: Flavor(default_region="default", bucket_stats=True),
    S3BrowserFlavors.MINIO: Flavor(default_region="us-east-1", bucket_stats=False),
    # Garage answers versioning and ACL reads with fixed stand-ins (never
    # versioned, owner has full control), so they say nothing and are skipped.
    S3BrowserFlavors.GARAGE: Flavor(
        default_region="garage",
        bucket_stats=False,
        bucket_parts=("location", "lifecycle_rules", "cors_rules"),
        object_parts=(),
    ),
}

# One listing page, whatever the client asks for: S3 itself caps at 1000.
MAX_PAGE_SIZE = 1000
DEFAULT_PAGE_SIZE = 200
# A prefix delete removes at most this many objects per request, so a request
# cannot run unbounded; the response says whether more remain.
MAX_DELETE_PER_REQUEST = 10000
DOWNLOAD_URL_TTL_SECONDS = 300
CONNECT_TIMEOUT_SECONDS = 5
# Sizes in the bucket list come from one HEAD per bucket: this many in
# parallel, and only for the first STATS_BUCKET_LIMIT buckets.
STATS_WORKERS = 8
STATS_BUCKET_LIMIT = 200
READ_TIMEOUT_SECONDS = 30

# S3 bucket naming rules (lower case, digits, dots and hyphens, 3-63).
BUCKET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
# What RGW itself accepts with rgw_relaxed_s3_bucket_names, or accepted from
# older clients: an existing bucket may be spelled so and must stay reachable.
# Addressing is path-style, so the name only ever lands in a URL path segment.
EXISTING_BUCKET_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,255}$")
MAX_KEY_LENGTH = 1024


class S3BrowserUnavailable(rf_exceptions.APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "s3_browser_unavailable"


class S3GatewayError(rf_exceptions.APIException):
    """An S3 error answered by the gateway, carried to the client with its code."""

    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "s3_error"

    def __init__(self, code: str, message: str, http_status: int):
        super().__init__(detail={"code": code, "detail": message})
        self.code = code
        self.status_code = http_status


class S3UploadTooLarge(rf_exceptions.APIException):
    status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
    default_code = "s3_upload_too_large"

    def __init__(self, limit: int):
        super().__init__(
            detail=_(
                "The file is larger than %(mb)s MB, the most the browser uploads. "
                "Use an S3 client for large files."
            )
            % {"mb": limit // (1024 * 1024)}
        )


class S3GatewayUnreachable(rf_exceptions.APIException):
    status_code = status.HTTP_502_BAD_GATEWAY
    default_code = "s3_gateway_unreachable"


class S3GatewayTimeout(rf_exceptions.APIException):
    status_code = status.HTTP_504_GATEWAY_TIMEOUT
    default_code = "s3_gateway_timeout"


# Gateway codes and the status the API answers with. Anything unknown is 400.
_ERROR_STATUS = {
    "NoSuchBucket": status.HTTP_404_NOT_FOUND,
    "NoSuchKey": status.HTTP_404_NOT_FOUND,
    "AccessDenied": status.HTTP_403_FORBIDDEN,
    "InvalidAccessKeyId": status.HTTP_403_FORBIDDEN,
    "SignatureDoesNotMatch": status.HTTP_403_FORBIDDEN,
    "UserSuspended": status.HTTP_403_FORBIDDEN,
    "QuotaExceeded": status.HTTP_409_CONFLICT,
    "BucketNotEmpty": status.HTTP_409_CONFLICT,
    "BucketAlreadyExists": status.HTTP_409_CONFLICT,
    "BucketAlreadyOwnedByYou": status.HTTP_409_CONFLICT,
    "TooManyBuckets": status.HTTP_409_CONFLICT,
    "PreconditionFailed": status.HTTP_409_CONFLICT,
    "MalformedResponse": status.HTTP_502_BAD_GATEWAY,
}


def is_enabled(offering: models.Offering) -> bool:
    return bool((offering.plugin_options or {}).get(PLUGIN_OPTION))


def flavor_name(offering: models.Offering) -> str:
    name = (offering.plugin_options or {}).get(FLAVOR_OPTION) or GENERIC
    return name if name in FLAVORS else GENERIC


def flavor_of(offering: models.Offering) -> Flavor:
    return FLAVORS[flavor_name(offering)]


def capabilities_of(offering: models.Offering) -> dict:
    """What the browser shows for this offering's gateway. For a generic one the
    detail parts are only candidates: those it refuses come back unavailable."""
    flavor = flavor_of(offering)
    return {
        "flavor": flavor_name(offering),
        "bucket_stats": flavor.bucket_stats,
        "bucket_parts": list(flavor.bucket_parts),
        "object_parts": list(flavor.object_parts),
    }


def _allowed_hosts() -> list[str]:
    raw = config.S3_BROWSER_ALLOWED_ENDPOINTS or ""
    return [
        entry.strip().lower() for entry in re.split(r"[\s,]+", raw) if entry.strip()
    ]


def _host_allowed(host: str, port: int | None) -> bool:
    """Whether ``host[:port]`` is on the staff allowlist.

    An entry is a host name or IP, optionally with ``:port``; an entry without a
    port admits the host on any port.
    """
    host = host.lower()
    for entry in _allowed_hosts():
        entry_host, entry_port = _split_entry(entry)
        if entry_host == host and (not entry_port or str(port) == entry_port):
            return True
    return False


def _split_entry(entry: str) -> tuple[str, str]:
    """``host``, ``host:port``, ``[v6]``, ``[v6]:port`` or a bare ``v6`` address,
    as (host, port) with the host spelled as urlsplit().hostname spells it."""
    if entry.startswith("["):
        address, _bracket, rest = entry[1:].partition("]")
        port = rest[1:] if rest.startswith(":") and rest[1:].isdigit() else ""
        return address, port
    if entry.count(":") == 1:
        name, _sep, port = entry.partition(":")
        if port.isdigit():
            return name, port
    return entry, ""


@dataclass(frozen=True)
class Gateway:
    endpoint: str
    region: str
    verify: bool


def gateway_for(resource: models.Resource) -> Gateway:
    """The S3 endpoint of a resource, refused unless staff allowlisted its host.

    Raises:
        S3BrowserUnavailable: no endpoint published, a non-http(s) one, or a host
            that is not on S3_BROWSER_ALLOWED_ENDPOINTS.
    """
    metadata = resource.backend_metadata or {}
    endpoint = str(metadata.get("s3_endpoint") or "").strip()
    if not endpoint:
        raise S3BrowserUnavailable(
            _("The resource has not published an S3 endpoint yet.")
        )
    parts = urlsplit(endpoint)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise S3BrowserUnavailable(_("The resource's S3 endpoint is not a URL."))
    if parts.username or parts.password:
        raise S3BrowserUnavailable(_("The resource's S3 endpoint carries credentials."))
    if not _host_allowed(parts.hostname, parts.port):
        raise S3BrowserUnavailable(
            _(
                "The S3 endpoint %(host)s is not on the S3_BROWSER_ALLOWED_ENDPOINTS "
                "list; staff must allow it before buckets can be browsed."
            )
            % {"host": parts.hostname}
        )
    if parts.scheme == "http" and not _is_private_ip(parts.hostname):
        raise S3BrowserUnavailable(
            _("The resource's S3 endpoint is plain HTTP; only HTTPS is browsed.")
        )
    # Spelled as a browser sends the Host header -- lower case, no default port --
    # so a presigned link is signed for the host the browser will name.
    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    default_port = {"https": 443, "http": 80}[parts.scheme]
    if parts.port and parts.port != default_port:
        host = f"{host}:{parts.port}"
    return Gateway(
        endpoint=f"{parts.scheme}://{host}",
        region=str(
            metadata.get("s3_region") or flavor_of(resource.offering).default_region
        ),
        verify=bool(config.S3_BROWSER_VERIFY_TLS),
    )


def _is_private_ip(host: str) -> bool:
    """Plain HTTP only to a private address: a lab gateway, never the internet."""
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


def client_for(api_key: models.ResourceApiKey) -> S3Client:
    """An S3 client acting as the key, at the resource's allowlisted endpoint."""
    gateway = gateway_for(api_key.resource)
    try:
        secret = encryption.decrypt_value(api_key.key_ciphertext)
    except InvalidToken:
        raise IncorrectStateException(
            _(
                "The stored API key cannot be decrypted with the configured "
                "encryption keys; check FIELD_ENCRYPTION_KEY and "
                "FIELD_ENCRYPTION_KEY_FALLBACKS."
            )
        )
    # Path-style addressing: self-hosted gateways often have no wildcard DNS
    # for virtual-hosted buckets.
    return S3Client(
        endpoint_url=gateway.endpoint,
        region_name=gateway.region,
        aws_access_key_id=api_key.client_id,
        aws_secret_access_key=secret,
        verify=gateway.verify,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        read_timeout=READ_TIMEOUT_SECONDS,
    )


def _call(fn, *args, **kwargs):
    """Run one S3 call, mapping its failures onto API errors."""
    try:
        return fn(*args, **kwargs)
    except S3ClientError as e:
        raise S3GatewayError(e.code, e.message, _ERROR_STATUS.get(e.code, 400))
    except S3TimeoutError as e:
        logger.warning("S3 gateway timed out: %s", e)
        raise S3GatewayTimeout(_("The S3 gateway did not answer in time."))
    except S3ConnectionError as e:
        logger.warning("S3 gateway unreachable: %s", e)
        raise S3GatewayUnreachable(_("The S3 gateway could not be reached."))


def validate_bucket(name: str) -> str:
    """A bucket that may already exist: the gateway's own, relaxed naming."""
    if (
        not isinstance(name, str)
        or not EXISTING_BUCKET_NAME_RE.match(name)
        or name in (".", "..")
    ):
        raise rf_exceptions.ValidationError({"bucket": _("Not a valid bucket name.")})
    return name


def validate_new_bucket(name: str) -> str:
    """A bucket to create: the strict S3 rules, which every gateway accepts."""
    if not isinstance(name, str) or not BUCKET_NAME_RE.match(name) or ".." in name:
        raise rf_exceptions.ValidationError({"bucket": _("Not a valid bucket name.")})
    return name


_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


def validate_key(key: str, field: str = "key", in_path: bool = True) -> str:
    """An object key the browser can act on.

    S3 allows any key, but two kinds cannot go through here: control characters
    (XML 1.0 cannot carry them in a multi-delete body) and, for keys sent in the
    URL path, "." or ".." segments, which HTTP clients and browsers remove
    before sending, so the request no longer matches its signature. Both are
    left to an S3 client.
    """
    if not isinstance(key, str) or not key or len(key) > MAX_KEY_LENGTH:
        raise rf_exceptions.ValidationError({field: _("Not a valid object key.")})
    if _CONTROL_CHARACTERS.search(key) or (
        in_path and any(part in (".", "..") for part in key.split("/"))
    ):
        raise rf_exceptions.ValidationError(
            {
                field: _(
                    "The browser cannot handle this name (control characters, "
                    "or a '.' or '..' folder); use an S3 client."
                )
            }
        )
    return key


def _int_header(headers: dict, name: str) -> int | None:
    try:
        return int(headers[name])
    except (KeyError, TypeError, ValueError):
        return None


NO_STATS = {"objects": None, "size_bytes": None}


def _stats_from(response: dict) -> dict:
    headers = response.get("ResponseMetadata", {}).get("HTTPHeaders", {})
    return {
        "objects": _int_header(headers, "x-rgw-object-count"),
        "size_bytes": _int_header(headers, "x-rgw-bytes-used"),
    }


def _bucket_stats(client, bucket: str, flavor: Flavor) -> dict:
    """Object count and bytes used; None where the gateway gives none."""
    if not flavor.bucket_stats:
        return dict(NO_STATS)
    try:
        # RGW answers with X-RGW-Object-Count and X-RGW-Bytes-Used when asked.
        response = _call(client.head_bucket, Bucket=bucket, ReadStats=True)
    except rf_exceptions.APIException as e:
        logger.info("No stats for bucket %s: %s", bucket, e)
        return dict(NO_STATS)
    return _stats_from(response)


# The bucket list costs up to four gateway calls per bucket, and the tab asks
# for it again on every visit; a short cache spares the gateway. Changes made
# through the browser drop it at once; others show within this many seconds.
BUCKET_LIST_CACHE_SECONDS = 30


def _bucket_list_cache_key(api_key) -> str:
    return f"s3-browser:buckets:{api_key.uuid.hex}"


def forget_bucket_list(api_key) -> None:
    cache.delete(_bucket_list_cache_key(api_key))


def list_buckets(api_key) -> list[dict]:
    cached = cache.get(_bucket_list_cache_key(api_key))
    if cached is not None:
        return cached
    buckets = _list_buckets(api_key)
    cache.set(_bucket_list_cache_key(api_key), buckets, BUCKET_LIST_CACHE_SECONDS)
    return buckets


def _list_buckets(api_key) -> list[dict]:
    client = client_for(api_key)
    flavor = flavor_of(api_key.resource.offering)
    response = _call(client.list_buckets)
    buckets = [
        {"name": bucket["Name"], "created": bucket.get("CreationDate")}
        for bucket in response.get("Buckets", [])
    ]
    measured = buckets[:STATS_BUCKET_LIMIT]
    with ThreadPoolExecutor(max_workers=STATS_WORKERS) as pool:
        summaries = list(
            pool.map(lambda b: _bucket_summary(client, b["name"], flavor), measured)
        )
    for bucket, summary in zip(measured, summaries):
        bucket.update(summary)
    for bucket in buckets[STATS_BUCKET_LIMIT:]:
        bucket.update(objects=None, size_bytes=None, versioning=None, is_public=None)
    return buckets


def _bucket_summary(client, bucket: str, flavor: Flavor) -> dict:
    """Size, versioning and whether anyone may read it: what a bucket row shows.
    A part the gateway will not give, or its flavor lacks, is None."""
    summary = _bucket_stats(client, bucket, flavor)
    summary["versioning"] = None
    if "versioning" in flavor.bucket_parts:
        try:
            versioning = _optional(client.get_bucket_versioning, Bucket=bucket) or {}
            summary["versioning"] = versioning.get("Status") or "Disabled"
        except _Unavailable:
            pass
    summary["is_public"] = None
    public = []
    try:
        if "acl" in flavor.bucket_parts:
            acl = _optional(client.get_bucket_acl, Bucket=bucket) or {}
            public.append(any(g["group"] for g in _grants(acl)))
        if "policy_public" in flavor.bucket_parts:
            status_ = _optional(client.get_bucket_policy_status, Bucket=bucket) or {}
            public.append(bool((status_.get("PolicyStatus") or {}).get("IsPublic")))
    except _Unavailable:
        public = []
    if public:
        summary["is_public"] = any(public)
    return summary


def quotas_of(resource: models.Resource) -> dict:
    """The resource's quotas as its site agent last published them.

    RGW does not report an account's quota to the account's own keys, so the
    agent's record is the only source; it is as current as the agent's last
    metadata sync. In account mode the storage limit is the account quota,
    otherwise the user quota; the bucket quota applies to every bucket.
    """
    quotas = (resource.backend_metadata or {}).get("quotas") or {}

    def limits(quota) -> dict | None:
        if not isinstance(quota, dict) or not _truthy(quota.get("enabled")):
            return None
        size, objects = (
            _positive(quota.get("max_size_bytes")),
            _positive(quota.get("max_objects")),
        )
        if size is None and objects is None:
            return None
        return {"max_size_bytes": size, "max_objects": objects}

    if "account_quota" in quotas:
        scope, total = "account", limits(quotas.get("account_quota"))
    else:
        scope, total = "user", limits(quotas.get("user_quota"))
    return {
        "scope": scope,
        "total": total,
        "per_bucket": limits(quotas.get("bucket_quota")),
    }


def _truthy(value) -> bool:
    return value is True or str(value).lower() == "true"


def _positive(value) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


# Codes that mean "this bucket has no such configuration", not a failure.
_NOT_CONFIGURED = {
    "NoSuchBucketPolicy",
    "NoSuchCORSConfiguration",
    "NoSuchLifecycleConfiguration",
    "NoSuchTagSet",
    "NoSuchPublicAccessBlockConfiguration",
    "ObjectLockConfigurationNotFoundError",
    "ServerSideEncryptionConfigurationNotFoundError",
}

_PUBLIC_GRANTEES = {
    "http://acs.amazonaws.com/groups/global/AllUsers": "all_users",
    "http://acs.amazonaws.com/groups/global/AuthenticatedUsers": "authenticated_users",
}


class _Unavailable(Exception):
    pass


class _Skipped(Exception):
    """A part the offering's flavor does not implement: never asked for."""


def _optional(fn, not_configured=_NOT_CONFIGURED, **kwargs):
    """A bucket configuration read: None when the bucket has none set; raises
    _Unavailable when the gateway refuses or lacks the call. An unreachable
    gateway still fails the whole request (502/504)."""
    try:
        response = _call(fn, **kwargs)
    except S3GatewayError as e:
        if e.code in not_configured:
            return None
        raise _Unavailable from e
    response.pop("ResponseMetadata", None)
    return response


def _grants(acl: dict) -> list[dict]:
    grants = []
    for grant in acl.get("Grants", []):
        grantee = grant.get("Grantee", {})
        uri = grantee.get("URI")
        grants.append(
            {
                "type": grantee.get("Type") or "",
                "grantee": grantee.get("ID")
                or uri
                or grantee.get("EmailAddress")
                or "",
                "display_name": grantee.get("DisplayName") or "",
                "group": _PUBLIC_GRANTEES.get(uri or ""),
                "permission": grant.get("Permission") or "",
            }
        )
    return grants


def _lifecycle_rules(config: dict) -> list[dict]:
    rules = []
    for rule in config.get("Rules", []):
        rule_filter = rule.get("Filter") or {}
        prefix = rule_filter.get("Prefix", rule.get("Prefix")) or (
            rule_filter.get("And") or {}
        ).get("Prefix", "")
        expiration = rule.get("Expiration") or {}
        noncurrent = rule.get("NoncurrentVersionExpiration") or {}
        abort = rule.get("AbortIncompleteMultipartUpload") or {}
        rules.append(
            {
                "id": rule.get("ID") or "",
                "enabled": rule.get("Status") == "Enabled",
                "prefix": prefix or "",
                "expiration_days": expiration.get("Days"),
                "noncurrent_expiration_days": noncurrent.get("NoncurrentDays"),
                "abort_multipart_days": abort.get("DaysAfterInitiation"),
                "transitions": len(rule.get("Transitions") or [])
                + len(rule.get("NoncurrentVersionTransitions") or []),
            }
        )
    return rules


def bucket_details(api_key, bucket: str) -> dict:
    """Everything the bucket's own key can read about its configuration.

    Each part is read on its own: one the gateway refuses or does not
    implement is listed in ``unavailable`` and the rest still answer. A part
    the offering's flavor lacks is never asked for, and is null.
    """
    validate_bucket(bucket)
    client = client_for(api_key)
    flavor = flavor_of(api_key.resource.offering)
    # Fails the request with 404 when the bucket is gone, so a part answered
    # with NoSuchBucket below means only "none set": Garage says so for CORS
    # and lifecycle.
    head = _call(client.head_bucket, Bucket=bucket, ReadStats=flavor.bucket_stats)
    stats = _stats_from(head) if flavor.bucket_stats else dict(NO_STATS)
    details: dict = {"bucket": bucket, **stats}
    unavailable: list[str] = []
    not_configured = _NOT_CONFIGURED | {"NoSuchBucket"}

    def read(name, fn, convert, **kwargs):
        details[name] = None
        if name not in flavor.bucket_parts:
            return
        try:
            response = _optional(
                fn, not_configured=not_configured, Bucket=bucket, **kwargs
            )
        except _Unavailable:
            unavailable.append(name)
            return
        if response is not None:
            details[name] = convert(response)

    read(
        "location",
        client.get_bucket_location,
        lambda r: r.get("LocationConstraint") or "",
    )
    read(
        "versioning",
        client.get_bucket_versioning,
        lambda r: {
            "status": r.get("Status") or "Disabled",
            "mfa_delete": r.get("MFADelete") == "Enabled",
        },
    )

    def object_lock(r):
        lock = r.get("ObjectLockConfiguration") or {}
        retention = (lock.get("Rule") or {}).get("DefaultRetention") or {}
        return {
            "enabled": lock.get("ObjectLockEnabled") == "Enabled",
            "mode": retention.get("Mode") or "",
            "days": retention.get("Days"),
            "years": retention.get("Years"),
        }

    read("object_lock", client.get_object_lock_configuration, object_lock)
    read(
        "acl",
        client.get_bucket_acl,
        lambda r: {
            "owner": (r.get("Owner") or {}).get("DisplayName")
            or (r.get("Owner") or {}).get("ID")
            or "",
            "grants": _grants(r),
        },
    )
    read("policy", client.get_bucket_policy, lambda r: _pretty_json(r.get("Policy")))
    read(
        "policy_public",
        client.get_bucket_policy_status,
        lambda r: bool((r.get("PolicyStatus") or {}).get("IsPublic")),
    )

    def public_access_block(r):
        block = r.get("PublicAccessBlockConfiguration") or {}
        return {
            "block_public_acls": bool(block.get("BlockPublicAcls")),
            "ignore_public_acls": bool(block.get("IgnorePublicAcls")),
            "block_public_policy": bool(block.get("BlockPublicPolicy")),
            "restrict_public_buckets": bool(block.get("RestrictPublicBuckets")),
        }

    read("public_access_block", client.get_public_access_block, public_access_block)
    read(
        "lifecycle_rules",
        client.get_bucket_lifecycle_configuration,
        _lifecycle_rules,
    )
    read(
        "encryption",
        client.get_bucket_encryption,
        lambda r: next(
            (
                (rule.get("ApplyServerSideEncryptionByDefault") or {}).get(
                    "SSEAlgorithm"
                )
                or ""
                for rule in (r.get("ServerSideEncryptionConfiguration") or {}).get(
                    "Rules", []
                )
            ),
            "",
        ),
    )
    read(
        "cors_rules",
        client.get_bucket_cors,
        lambda r: [
            {
                "allowed_origins": rule.get("AllowedOrigins") or [],
                "allowed_methods": rule.get("AllowedMethods") or [],
            }
            for rule in r.get("CORSRules", [])
        ],
    )
    read(
        "tags",
        client.get_bucket_tagging,
        lambda r: [
            {"key": tag.get("Key", ""), "value": tag.get("Value", "")}
            for tag in r.get("TagSet", [])
        ],
    )

    public_groups = sorted(
        {g["group"] for g in (details.get("acl") or {}).get("grants", []) if g["group"]}
    )
    details["public_acl_groups"] = public_groups
    details["is_public"] = bool(public_groups) or bool(details.get("policy_public"))
    details["unavailable"] = unavailable
    return details


def _pretty_json(raw) -> str:
    if not raw:
        return ""
    try:
        return json.dumps(json.loads(raw), indent=2, sort_keys=True)
    except (TypeError, ValueError):
        return str(raw)


def list_objects(
    api_key,
    bucket: str,
    prefix: str = "",
    continuation_token: str = "",
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict:
    """One page of a bucket listing; common prefixes under ``prefix`` read as folders."""
    params = {
        "Bucket": validate_bucket(bucket),
        "Prefix": prefix or "",
        "Delimiter": "/",
        "MaxKeys": max(1, min(int(page_size or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE)),
    }
    if continuation_token:
        params["ContinuationToken"] = continuation_token
    response = _call(client_for(api_key).list_objects_v2, **params)
    return {
        "bucket": bucket,
        "prefix": prefix or "",
        "folders": [p["Prefix"] for p in response.get("CommonPrefixes", [])],
        "objects": [
            {
                "key": item["Key"],
                "size": item.get("Size", 0),
                "modified": item.get("LastModified"),
                "etag": str(item.get("ETag", "")).strip('"'),
                "storage_class": item.get("StorageClass") or "STANDARD",
            }
            for item in response.get("Contents", [])
            # The "folder" placeholder object some clients write for a prefix.
            if item["Key"] != (prefix or "")
        ],
        "next_continuation_token": response.get("NextContinuationToken") or "",
        "is_truncated": bool(response.get("IsTruncated")),
    }


# How many versions of one object the details count before giving up; a
# key rewritten every minute for years should not page the whole history.
MAX_VERSIONS_COUNTED = 1000


def object_details(api_key, bucket: str, key: str) -> dict:
    """One object's headers, ACL, tags and version count, as its key reads them.

    The headers come from HEAD and fail the request when the object is gone;
    ACL, tags and versions are read on their own and listed in
    ``unavailable`` when the gateway refuses them.
    """
    validate_bucket(bucket)
    validate_key(key)
    client = client_for(api_key)
    parts = flavor_of(api_key.resource.offering).object_parts
    head = _call(client.head_object, Bucket=bucket, Key=key)
    etag = str(head.get("ETag", "")).strip('"')
    details: dict = {
        "bucket": bucket,
        "key": key,
        "size": head.get("ContentLength", 0),
        "modified": head.get("LastModified"),
        "etag": etag,
        # S3 marks a multipart upload's ETag with its part count: "<md5>-<parts>".
        "multipart_parts": int(etag.rsplit("-", 1)[1])
        if "-" in etag and etag.rsplit("-", 1)[1].isdigit()
        else None,
        "content_type": head.get("ContentType") or "",
        "content_encoding": head.get("ContentEncoding") or "",
        "content_disposition": head.get("ContentDisposition") or "",
        "cache_control": head.get("CacheControl") or "",
        "storage_class": head.get("StorageClass") or "STANDARD",
        "version_id": head.get("VersionId") or "",
        "encryption": head.get("ServerSideEncryption") or "",
        "metadata": [
            {"key": name, "value": value}
            for name, value in sorted((head.get("Metadata") or {}).items())
        ],
        "lock_mode": head.get("ObjectLockMode") or "",
        "retain_until": head.get("ObjectLockRetainUntilDate"),
        "legal_hold": head.get("ObjectLockLegalHoldStatus") == "ON",
    }
    unavailable: list[str] = []
    details.update(
        acl=None,
        is_public=None,
        tags=None,
        versions=None,
        delete_markers=None,
        versions_truncated=False,
    )

    try:
        if "acl" not in parts:
            raise _Skipped
        acl = _optional(client.get_object_acl, Bucket=bucket, Key=key) or {}
        grants = _grants(acl)
        details["acl"] = {
            "owner": (acl.get("Owner") or {}).get("DisplayName")
            or (acl.get("Owner") or {}).get("ID")
            or "",
            "grants": grants,
        }
        details["is_public"] = any(grant["group"] for grant in grants)
    except _Skipped:
        pass
    except _Unavailable:
        unavailable.append("acl")

    try:
        if "tags" not in parts:
            raise _Skipped
        tagging = _optional(client.get_object_tagging, Bucket=bucket, Key=key) or {}
        details["tags"] = [
            {"key": tag.get("Key", ""), "value": tag.get("Value", "")}
            for tag in tagging.get("TagSet", [])
        ]
    except _Skipped:
        pass
    except _Unavailable:
        unavailable.append("tags")

    try:
        if "versions" not in parts:
            raise _Skipped
        listing = (
            _optional(
                client.list_object_versions,
                Bucket=bucket,
                Prefix=key,
                MaxKeys=MAX_VERSIONS_COUNTED,
            )
            or {}
        )
        # Prefix matches longer keys too ("a" lists "a.txt"); count this key only.
        details["versions"] = sum(
            1 for v in listing.get("Versions", []) if v.get("Key") == key
        )
        details["delete_markers"] = sum(
            1 for m in listing.get("DeleteMarkers", []) if m.get("Key") == key
        )
        details["versions_truncated"] = bool(listing.get("IsTruncated"))
    except _Skipped:
        pass
    except _Unavailable:
        unavailable.append("versions")

    details["unavailable"] = unavailable
    return details


def download_url(api_key, bucket: str, key: str) -> dict:
    client = client_for(api_key)
    validate_bucket(bucket)
    validate_key(key)
    # Fails here, not in the user's browser, when the object is gone.
    head = _call(client.head_object, Bucket=bucket, Key=key)
    filename = key.rsplit("/", 1)[-1] or "download"
    url = client.presign_get_object(
        Bucket=bucket,
        Key=key,
        ExpiresIn=DOWNLOAD_URL_TTL_SECONDS,
        ResponseContentDisposition=_attachment(filename),
    )
    return {
        "url": url,
        "expires_in": DOWNLOAD_URL_TTL_SECONDS,
        "size": head.get("ContentLength", 0),
    }


def _attachment(name: str) -> str:
    """Content-Disposition for a download (RFC 6266): an ASCII ``filename`` for
    old clients, and the real name, UTF-8 and percent-encoded, in ``filename*``.
    A header can only carry ASCII, so a raw non-ASCII name arrives garbled."""
    ascii_name = "".join(
        char if 32 <= ord(char) < 127 and char not in '"\\' else "_" for char in name
    )
    return (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(name, safe='')}"
    )


def create_bucket(api_key, bucket: str) -> None:
    _call(client_for(api_key).create_bucket, Bucket=validate_new_bucket(bucket))
    forget_bucket_list(api_key)


def delete_bucket(api_key, bucket: str) -> None:
    _call(client_for(api_key).delete_bucket, Bucket=validate_bucket(bucket))
    forget_bucket_list(api_key)


def delete_objects(api_key, bucket: str, keys: list[str]) -> dict:
    """Delete the named objects (current versions only)."""
    validate_bucket(bucket)
    keys = [validate_key(k, "keys", in_path=False) for k in keys][
        :MAX_DELETE_PER_REQUEST
    ]
    client = client_for(api_key)
    deleted, errors, stopped = _delete_batches(client, bucket, keys)
    forget_bucket_list(api_key)
    return {"deleted": deleted, "errors": errors, "remaining": stopped}


def delete_prefix(api_key, bucket: str, prefix: str) -> dict:
    """Delete up to MAX_DELETE_PER_REQUEST objects under ``prefix``.

    ``remaining`` tells the caller to repeat the request for the rest.
    """
    validate_bucket(bucket)
    validate_key(prefix, "prefix", in_path=False)
    client = client_for(api_key)
    keys: list[str] = []
    token = ""
    truncated = True
    while truncated and len(keys) < MAX_DELETE_PER_REQUEST:
        params = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
        if token:
            params["ContinuationToken"] = token
        page = _call(client.list_objects_v2, **params)
        keys.extend(item["Key"] for item in page.get("Contents", []))
        token = page.get("NextContinuationToken") or ""
        truncated = bool(page.get("IsTruncated")) and bool(token)
    remaining = truncated or len(keys) > MAX_DELETE_PER_REQUEST
    deleted, errors, stopped = _delete_batches(
        client, bucket, keys[:MAX_DELETE_PER_REQUEST]
    )
    forget_bucket_list(api_key)
    return {"deleted": deleted, "errors": errors, "remaining": remaining or stopped}


def _delete_batches(client, bucket: str, keys: list[str]) -> tuple[int, list, bool]:
    """Delete in batches of 1000. A batch that fails after others went through
    ends the run with what was deleted so far, the failure as an error, and
    ``stopped`` set, so the caller still reports (and audits) the deletions.
    A failure of the very first batch is raised as it is."""
    deleted = 0
    errors: list[dict] = []
    for start in range(0, len(keys), 1000):
        batch = keys[start : start + 1000]
        try:
            response = _call(
                client.delete_objects,
                Bucket=bucket,
                Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
            )
        except rf_exceptions.APIException as e:
            if start == 0:
                raise
            detail = e.detail if isinstance(e.detail, dict) else {}
            errors.append(
                {
                    "key": None,
                    "code": str(detail.get("code") or e.default_code),
                    "detail": str(detail.get("detail") or e.detail),
                }
            )
            return deleted, errors, True
        failed = response.get("Errors", [])
        errors.extend(
            {"key": e.get("Key"), "code": e.get("Code"), "detail": e.get("Message")}
            for e in failed
        )
        deleted += len(batch) - len(failed)
    return deleted, errors, False


def max_upload_bytes() -> int:
    return int(config.S3_BROWSER_MAX_UPLOAD_SIZE_MB) * 1024 * 1024


def upload(
    api_key,
    bucket: str,
    key: str,
    fileobj: IO[bytes],
    size: int,
    content_type: str = "",
    overwrite: bool = False,
) -> None:
    validate_bucket(bucket)
    validate_key(key)
    client = client_for(api_key)
    if not overwrite and _object_exists(client, bucket, key):
        raise _object_exists_error()
    try:
        # If-None-Match also closes the race between that check and the write,
        # on gateways that honour it; Garage ignores it, hence the check.
        _call(
            client.put_object,
            Bucket=bucket,
            Key=key,
            Body=fileobj,
            ContentLength=size,
            ContentType=content_type or None,
            IfNoneMatch=None if overwrite else "*",
        )
    except S3GatewayError as e:
        if e.code != "PreconditionFailed":
            raise
        raise _object_exists_error()
    forget_bucket_list(api_key)


def _object_exists(client, bucket: str, key: str) -> bool:
    """Whether ``key`` exists. A key that may write but not read cannot tell,
    and goes ahead on If-None-Match alone."""
    try:
        _call(client.head_object, Bucket=bucket, Key=key)
    except S3GatewayError as e:
        if e.status_code in (status.HTTP_404_NOT_FOUND, status.HTTP_403_FORBIDDEN):
            return False
        raise
    return True


def _object_exists_error() -> S3GatewayError:
    return S3GatewayError(
        "ObjectExists",
        _("A file with this name already exists; confirm to replace it."),
        status.HTTP_409_CONFLICT,
    )
