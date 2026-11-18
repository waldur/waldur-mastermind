"""A small S3 client for the operations the bucket browser uses.

It speaks plain S3 over ``requests`` with path-style addressing, signs with
SigV4 (``signer.py``) and parses responses with ``defusedxml``. Method names,
keyword arguments and response dictionaries follow boto3's for the same
operations, so callers read like boto3 code; only what the browser needs is
implemented. Responses carry only the fields the browser reads.

Nothing here is sent unless the operation needs it: no checksums beyond the
Content-MD5 that DeleteObjects requires, so gateways that refuse the newer
CRC headers (Ceph before Squid) work unchanged.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import IO, Any
from urllib.parse import quote, unquote, urlsplit

# Building XML only; every response is parsed with defusedxml.
from xml.etree.ElementTree import Element, SubElement, tostring  # nosec B405

import requests
from defusedxml import DefusedXmlException, ElementTree
from django.utils.dateparse import parse_datetime
from requests.adapters import HTTPAdapter

from . import signer

S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
XSI_TYPE = "{http://www.w3.org/2001/XMLSchema-instance}type"
# Methods retried once on a dropped connection or a 500/503: they change
# nothing, or change it the same way twice.
_RETRIED_METHODS = ("GET", "HEAD", "DELETE")
_RETRIED_STATUSES = (500, 503)

# One connection pool for every client, sized for the bucket list's parallel
# reads: a client per API request would otherwise open (and leak until garbage
# collection) a pool per request. urllib3's pool is thread-safe.
_SESSION = requests.Session()
_SESSION.mount("https://", HTTPAdapter(pool_connections=16, pool_maxsize=16))
_SESSION.mount("http://", HTTPAdapter(pool_connections=16, pool_maxsize=16))


class S3ClientError(Exception):
    """An error the gateway answered with: an S3 error code and HTTP status."""

    def __init__(self, code: str, message: str, status: int):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status


class S3ConnectionError(Exception):
    """The gateway could not be reached, or the TLS handshake failed."""


class S3TimeoutError(Exception):
    """The gateway did not answer in time."""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(node: Element | None, name: str) -> Element | None:
    if node is None:
        return None
    for child in node:
        if _local(child.tag) == name:
            return child
    return None


def _children(node: Element | None, name: str) -> list[Element]:
    if node is None:
        return []
    return [child for child in node if _local(child.tag) == name]


def _text(node: Element | None, name: str, default: str = "") -> str:
    child = _child(node, name)
    return (child.text or "") if child is not None else default


def _int(node: Element | None, name: str) -> int | None:
    value = _text(node, name)
    return int(value) if value.strip().lstrip("-").isdigit() else None


def _bool(node: Element | None, name: str) -> bool:
    return _text(node, name).strip().lower() == "true"


def _iso(value: str) -> datetime | None:
    return parse_datetime(value) if value else None


def _http_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


def _grants(root: Element) -> dict[str, Any]:
    owner = _child(root, "Owner")
    grants = []
    for grant in _children(_child(root, "AccessControlList"), "Grant"):
        grantee = _child(grant, "Grantee")
        entry = {"Type": grantee.get(XSI_TYPE, "") if grantee is not None else ""}
        for name in ("ID", "DisplayName", "URI", "EmailAddress"):
            value = _text(grantee, name)
            if value:
                entry[name] = value
        grants.append({"Grantee": entry, "Permission": _text(grant, "Permission")})
    return {
        "Owner": {"ID": _text(owner, "ID"), "DisplayName": _text(owner, "DisplayName")},
        "Grants": grants,
    }


def _tags(root: Element) -> dict[str, Any]:
    return {
        "TagSet": [
            {"Key": _text(tag, "Key"), "Value": _text(tag, "Value")}
            for tag in _children(_child(root, "TagSet"), "Tag")
        ]
    }


class S3Client:
    """Path-style S3 at one endpoint, acting as one access key."""

    def __init__(
        self,
        *,
        endpoint_url: str,
        region_name: str,
        aws_access_key_id: str,
        aws_secret_access_key: str,
        verify: bool = True,
        connect_timeout: float = 5,
        read_timeout: float = 30,
    ):
        parts = urlsplit(endpoint_url)
        self.scheme = parts.scheme
        self.netloc = parts.netloc
        self.region = region_name or "us-east-1"
        self.credentials = signer.Credentials(aws_access_key_id, aws_secret_access_key)
        self.verify = verify
        self.timeout = (connect_timeout, read_timeout)
        self.session = _SESSION

    # --- transport -----------------------------------------------------------

    @staticmethod
    def _path(bucket: str | None, key: str | None) -> str:
        path = "/"
        if bucket:
            path += quote(bucket, safe="")
            if key is not None:
                path += "/" + quote(key, safe="/~")
        return path

    @staticmethod
    def _query(params: dict[str, Any] | None) -> str:
        """Percent-encoded and sorted, as sent and as signed; None drops a
        parameter and "" sends it bare (``?acl=``)."""
        pairs = sorted(
            (signer.queryencode(name), signer.queryencode(str(value)))
            for name, value in (params or {}).items()
            if value is not None
        )
        return "&".join(f"{name}={value}" for name, value in pairs)

    def _request(
        self,
        method: str,
        bucket: str | None = None,
        key: str | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        body: bytes | IO[bytes] = b"",
        payload_hash: str | None = None,
    ) -> requests.Response:
        url = urlsplit(
            f"{self.scheme}://{self.netloc}{self._path(bucket, key)}"
            + (f"?{query}" if (query := self._query(params)) else "")
        )
        if payload_hash is None:
            payload_hash = signer.sha256_hash(body if isinstance(body, bytes) else b"")
        attempts = 2 if method in _RETRIED_METHODS else 1
        response: requests.Response | None = None
        retried = False
        for attempt in range(attempts):
            now = datetime.now(UTC)
            signed = {
                "Host": self.netloc,
                "X-Amz-Date": signer.to_amz_date(now),
                "X-Amz-Content-Sha256": payload_hash,
                **(headers or {}),
            }
            signer.sign_v4_s3(
                method=method,
                url=url,
                region=self.region,
                headers=signed,
                credentials=self.credentials,
                content_sha256=payload_hash,
                date=now,
            )
            try:
                response = self.session.request(
                    method,
                    url.geturl(),
                    headers=signed,
                    data=body or None,
                    timeout=self.timeout,
                    verify=self.verify,
                    allow_redirects=False,
                )
            except (requests.ConnectTimeout, requests.ReadTimeout) as e:
                if attempt + 1 < attempts:
                    continue
                raise S3TimeoutError(str(e)) from e
            except requests.ConnectionError as e:
                if attempt + 1 < attempts:
                    continue
                raise S3ConnectionError(str(e)) from e
            except requests.RequestException as e:
                # A malformed header or URL (from a provider-set region, say),
                # or a body cut off mid-transfer: no answer to act on.
                raise S3ConnectionError(str(e)) from e
            if response.status_code in _RETRIED_STATUSES and attempt + 1 < attempts:
                continue
            retried = attempt > 0
            break
        if response is None:  # unreachable: each attempt answers or raises
            raise S3ConnectionError("The gateway gave no response.")
        if response.status_code == 404 and method == "DELETE" and retried:
            # The first attempt took effect before its answer was lost.
            return response
        if response.status_code >= 300:
            raise self._error(method, bucket, key, response)
        return response

    @staticmethod
    def _error(method, bucket, key, response: requests.Response) -> S3ClientError:
        status = response.status_code
        code, message = "", ""
        if response.content:
            try:
                root = ElementTree.fromstring(response.content)
                code, message = _text(root, "Code"), _text(root, "Message")
            except (ElementTree.ParseError, DefusedXmlException):
                pass
        if not code:
            # HEAD answers carry no body: name the error from the status.
            code = {
                404: "NoSuchKey" if key is not None else "NoSuchBucket",
                403: "AccessDenied",
                301: "PermanentRedirect",
            }.get(status, str(status))
        return S3ClientError(code, message or code, status)

    @staticmethod
    def _xml(response: requests.Response) -> Element:
        try:
            return ElementTree.fromstring(response.content)
        except (ElementTree.ParseError, DefusedXmlException) as e:
            raise S3ClientError(
                "MalformedResponse",
                f"The gateway's answer is not readable XML: {e}",
                502,
            ) from e

    # --- buckets -------------------------------------------------------------

    def list_buckets(self) -> dict[str, Any]:
        root = self._xml(self._request("GET"))
        return {
            "Buckets": [
                {
                    "Name": _text(bucket, "Name"),
                    "CreationDate": _iso(_text(bucket, "CreationDate")),
                }
                for bucket in _children(_child(root, "Buckets"), "Bucket")
            ]
        }

    def head_bucket(self, Bucket: str, ReadStats: bool = False) -> dict[str, Any]:
        """``ReadStats`` asks RGW for X-RGW-Object-Count and X-RGW-Bytes-Used."""
        response = self._request(
            "HEAD", Bucket, params={"read-stats": "true"} if ReadStats else None
        )
        return {
            "ResponseMetadata": {
                "HTTPHeaders": {k.lower(): v for k, v in response.headers.items()}
            }
        }

    def create_bucket(self, Bucket: str) -> dict[str, Any]:
        self._request("PUT", Bucket)
        return {}

    def delete_bucket(self, Bucket: str) -> dict[str, Any]:
        self._request("DELETE", Bucket)
        return {}

    def _bucket_xml(self, bucket: str, subresource: str) -> Element:
        return self._xml(self._request("GET", bucket, params={subresource: ""}))

    def get_bucket_location(self, Bucket: str) -> dict[str, Any]:
        root = self._bucket_xml(Bucket, "location")
        return {"LocationConstraint": root.text or ""}

    def get_bucket_versioning(self, Bucket: str) -> dict[str, Any]:
        root = self._bucket_xml(Bucket, "versioning")
        result = {}
        for name in ("Status", "MFADelete"):
            if _text(root, name):
                result[name] = _text(root, name)
        return result

    def get_object_lock_configuration(self, Bucket: str) -> dict[str, Any]:
        root = self._bucket_xml(Bucket, "object-lock")
        retention = _child(_child(root, "Rule"), "DefaultRetention")
        config: dict[str, Any] = {"ObjectLockEnabled": _text(root, "ObjectLockEnabled")}
        if retention is not None:
            config["Rule"] = {
                "DefaultRetention": {
                    "Mode": _text(retention, "Mode"),
                    "Days": _int(retention, "Days"),
                    "Years": _int(retention, "Years"),
                }
            }
        return {"ObjectLockConfiguration": config}

    def get_bucket_acl(self, Bucket: str) -> dict[str, Any]:
        return _grants(self._bucket_xml(Bucket, "acl"))

    def get_bucket_policy(self, Bucket: str) -> dict[str, Any]:
        response = self._request("GET", Bucket, params={"policy": ""})
        return {"Policy": response.text}

    def get_bucket_policy_status(self, Bucket: str) -> dict[str, Any]:
        root = self._bucket_xml(Bucket, "policyStatus")
        return {"PolicyStatus": {"IsPublic": _bool(root, "IsPublic")}}

    def get_public_access_block(self, Bucket: str) -> dict[str, Any]:
        root = self._bucket_xml(Bucket, "publicAccessBlock")
        return {
            "PublicAccessBlockConfiguration": {
                name: _bool(root, name)
                for name in (
                    "BlockPublicAcls",
                    "IgnorePublicAcls",
                    "BlockPublicPolicy",
                    "RestrictPublicBuckets",
                )
            }
        }

    def get_bucket_lifecycle_configuration(self, Bucket: str) -> dict[str, Any]:
        root = self._bucket_xml(Bucket, "lifecycle")
        rules = []
        for rule in _children(root, "Rule"):
            rule_filter = _child(rule, "Filter")
            entry: dict[str, Any] = {
                "ID": _text(rule, "ID"),
                "Status": _text(rule, "Status"),
            }
            if _child(rule, "Prefix") is not None:
                entry["Prefix"] = _text(rule, "Prefix")
            if rule_filter is not None:
                filter_: dict[str, Any] = {"Prefix": _text(rule_filter, "Prefix")}
                if (both := _child(rule_filter, "And")) is not None:
                    filter_["And"] = {"Prefix": _text(both, "Prefix")}
                entry["Filter"] = filter_
            for name, field in (
                ("Expiration", "Days"),
                ("NoncurrentVersionExpiration", "NoncurrentDays"),
                ("AbortIncompleteMultipartUpload", "DaysAfterInitiation"),
            ):
                if (node := _child(rule, name)) is not None:
                    entry[name] = {field: _int(node, field)}
            entry["Transitions"] = _children(rule, "Transition")
            entry["NoncurrentVersionTransitions"] = _children(
                rule, "NoncurrentVersionTransition"
            )
            rules.append(entry)
        return {"Rules": rules}

    def get_bucket_encryption(self, Bucket: str) -> dict[str, Any]:
        root = self._bucket_xml(Bucket, "encryption")
        return {
            "ServerSideEncryptionConfiguration": {
                "Rules": [
                    {
                        "ApplyServerSideEncryptionByDefault": {
                            "SSEAlgorithm": _text(
                                _child(rule, "ApplyServerSideEncryptionByDefault"),
                                "SSEAlgorithm",
                            )
                        }
                    }
                    for rule in _children(root, "Rule")
                ]
            }
        }

    def get_bucket_cors(self, Bucket: str) -> dict[str, Any]:
        root = self._bucket_xml(Bucket, "cors")
        return {
            "CORSRules": [
                {
                    "AllowedOrigins": [
                        node.text or "" for node in _children(rule, "AllowedOrigin")
                    ],
                    "AllowedMethods": [
                        node.text or "" for node in _children(rule, "AllowedMethod")
                    ],
                }
                for rule in _children(root, "CORSRule")
            ]
        }

    def get_bucket_tagging(self, Bucket: str) -> dict[str, Any]:
        return _tags(self._bucket_xml(Bucket, "tagging"))

    # --- objects -------------------------------------------------------------

    def list_objects_v2(
        self,
        Bucket: str,
        Prefix: str = "",
        Delimiter: str | None = None,
        MaxKeys: int = 1000,
        ContinuationToken: str | None = None,
    ) -> dict[str, Any]:
        root = self._xml(
            self._request(
                "GET",
                Bucket,
                params={
                    "list-type": "2",
                    "prefix": Prefix,
                    "delimiter": Delimiter,
                    "max-keys": MaxKeys,
                    "continuation-token": ContinuationToken or None,
                    # Keys may hold characters XML 1.0 cannot carry; asked
                    # for, the gateway percent-encodes them.
                    "encoding-type": "url",
                },
            )
        )
        return {
            "Contents": [
                {
                    "Key": unquote(_text(item, "Key")),
                    "Size": _int(item, "Size") or 0,
                    "LastModified": _iso(_text(item, "LastModified")),
                    "ETag": _text(item, "ETag"),
                    "StorageClass": _text(item, "StorageClass"),
                }
                for item in _children(root, "Contents")
            ],
            "CommonPrefixes": [
                {"Prefix": unquote(_text(node, "Prefix"))}
                for node in _children(root, "CommonPrefixes")
            ],
            "NextContinuationToken": _text(root, "NextContinuationToken"),
            "IsTruncated": _bool(root, "IsTruncated"),
        }

    def list_object_versions(
        self, Bucket: str, Prefix: str = "", MaxKeys: int = 1000
    ) -> dict[str, Any]:
        root = self._xml(
            self._request(
                "GET",
                Bucket,
                params={
                    "versions": "",
                    "prefix": Prefix,
                    "max-keys": MaxKeys,
                    "encoding-type": "url",
                },
            )
        )
        return {
            "Versions": [
                {
                    "Key": unquote(_text(node, "Key")),
                    "VersionId": _text(node, "VersionId"),
                }
                for node in _children(root, "Version")
            ],
            "DeleteMarkers": [
                {
                    "Key": unquote(_text(node, "Key")),
                    "VersionId": _text(node, "VersionId"),
                }
                for node in _children(root, "DeleteMarker")
            ],
            "IsTruncated": _bool(root, "IsTruncated"),
        }

    def head_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        headers = self._request("HEAD", Bucket, Key).headers
        result: dict[str, Any] = {
            "ContentLength": int(headers.get("Content-Length") or 0),
            "LastModified": _http_date(headers.get("Last-Modified")),
            "ETag": headers.get("ETag", ""),
            "Metadata": {
                name[len("x-amz-meta-") :]: value
                for name, value in headers.items()
                if name.lower().startswith("x-amz-meta-")
            },
            "ObjectLockRetainUntilDate": _iso(
                headers.get("x-amz-object-lock-retain-until-date", "")
            ),
        }
        for field, header in (
            ("ContentType", "Content-Type"),
            ("ContentEncoding", "Content-Encoding"),
            ("ContentDisposition", "Content-Disposition"),
            ("CacheControl", "Cache-Control"),
            ("StorageClass", "x-amz-storage-class"),
            ("VersionId", "x-amz-version-id"),
            ("ServerSideEncryption", "x-amz-server-side-encryption"),
            ("ObjectLockMode", "x-amz-object-lock-mode"),
            ("ObjectLockLegalHoldStatus", "x-amz-object-lock-legal-hold"),
        ):
            if headers.get(header):
                result[field] = headers[header]
        return result

    def get_object_acl(self, Bucket: str, Key: str) -> dict[str, Any]:
        return _grants(self._xml(self._request("GET", Bucket, Key, {"acl": ""})))

    def get_object_tagging(self, Bucket: str, Key: str) -> dict[str, Any]:
        return _tags(self._xml(self._request("GET", Bucket, Key, {"tagging": ""})))

    def put_object(
        self,
        Bucket: str,
        Key: str,
        Body: IO[bytes],
        ContentLength: int,
        ContentType: str | None = None,
        IfNoneMatch: str | None = None,
    ) -> dict[str, Any]:
        """Streams the body; its hash is left out of the signature
        (UNSIGNED-PAYLOAD), which every S3 gateway accepts. ``IfNoneMatch="*"``
        makes the gateway refuse (412 PreconditionFailed) a key that exists."""
        headers = {"Content-Length": str(ContentLength)}
        if ContentType:
            headers["Content-Type"] = ContentType
        if IfNoneMatch:
            headers["If-None-Match"] = IfNoneMatch
        response = self._request(
            "PUT",
            Bucket,
            Key,
            headers=headers,
            body=Body,
            payload_hash=signer.UNSIGNED_PAYLOAD,
        )
        return {"ETag": response.headers.get("ETag", "")}

    def delete_objects(self, Bucket: str, Delete: dict[str, Any]) -> dict[str, Any]:
        """Multi-object delete; S3 requires its Content-MD5."""
        root = Element("Delete")
        if Delete.get("Quiet"):
            SubElement(root, "Quiet").text = "true"
        for item in Delete.get("Objects", []):
            SubElement(SubElement(root, "Object"), "Key").text = item["Key"]
        body = tostring(root, encoding="utf-8")
        md5 = base64.b64encode(hashlib.md5(body, usedforsecurity=False).digest())
        result = self._xml(
            self._request(
                "POST",
                Bucket,
                params={"delete": ""},
                headers={
                    "Content-MD5": md5.decode(),
                    "Content-Type": "application/xml",
                },
                body=body,
            )
        )
        return {
            "Deleted": [
                {"Key": _text(node, "Key")} for node in _children(result, "Deleted")
            ],
            "Errors": [
                {
                    "Key": _text(node, "Key"),
                    "Code": _text(node, "Code"),
                    "Message": _text(node, "Message"),
                }
                for node in _children(result, "Error")
            ],
        }

    def presign_get_object(
        self,
        Bucket: str,
        Key: str,
        ExpiresIn: int,
        ResponseContentDisposition: str | None = None,
    ) -> str:
        url = urlsplit(
            f"{self.scheme}://{self.netloc}{self._path(Bucket, Key)}"
            + (
                "?"
                + self._query(
                    {"response-content-disposition": ResponseContentDisposition}
                )
                if ResponseContentDisposition
                else ""
            )
        )
        return signer.presign_v4(
            method="GET",
            url=url,
            region=self.region,
            credentials=self.credentials,
            date=datetime.now(UTC),
            expires=ExpiresIn,
        ).geturl()
