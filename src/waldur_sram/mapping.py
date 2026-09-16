"""Translate SRAM's SCIM resources to and from Waldur's SCIM representation.

SRAM (SURFscz/SBS ``server/scim/user_template.py`` and ``group_template.py``)
sends standard SCIM resources plus its own extension blocks:

- Users carry SSH keys base64-encoded in ``x509Certificates`` and identity
  attributes in ``urn:mace:surf.nl:sram:scim:extension:User``.
- Groups carry the SRAM URN, description, labels and links in
  ``urn:mace:surf.nl:sram:scim:extension:Group``.

Incoming users are rewritten into the Waldur extension shape so the generic
inbound SCIM code applies the usual attribute-source and SSH-key policies.
Outgoing resources echo SRAM's own fields back, because SBS compares them with
its data on every sweep and re-sends anything that differs.
"""

from __future__ import annotations

import base64
import binascii
import copy
import logging

from django.core.exceptions import ValidationError

from waldur_core.core.validators import validate_ssh_public_key
from waldur_core.users.scim.server.mapping import (
    WALDUR_USER_EXTENSION_URN,
    waldur_to_scim_user,
)
from waldur_core.users.scim.server.ssh_keys import SSH_KEYS_ATTRIBUTE

from . import models

logger = logging.getLogger(__name__)

SRAM_USER_EXTENSION_URN = "urn:mace:surf.nl:sram:scim:extension:User"
SRAM_GROUP_EXTENSION_URN = "urn:mace:surf.nl:sram:scim:extension:Group"
CORE_GROUP_URN = "urn:ietf:params:scim:schemas:core:2.0:Group"


def user_location(user) -> str:
    """Relative to the SRAM SCIM base: SBS requests ``{scim_url}{location}``."""
    return f"/Users/{user.uuid.hex}"


def group_location(group: models.SramGroup) -> str:
    return f"/Groups/{group.uuid.hex}"


def _decode_ssh_keys(certificates) -> list[dict]:
    """``x509Certificates`` → Waldur ``sshPublicKeys`` entries.

    Keys that do not decode or validate are skipped with a warning: one bad key
    must not block provisioning of the account.
    """
    entries = []
    for item in certificates or []:
        raw = item.get("value") if isinstance(item, dict) else None
        if not raw:
            continue
        try:
            public_key = base64.b64decode(raw, validate=True).decode("utf-8").strip()
        except (binascii.Error, UnicodeDecodeError):
            logger.warning("SRAM: skipping an SSH key that is not valid base64.")
            continue
        try:
            validate_ssh_public_key(public_key)
        except ValidationError:
            logger.warning("SRAM: skipping an SSH key Waldur does not accept.")
            continue
        entries.append({"value": public_key})
    return entries


def _split_affiliations(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        items = value
    else:
        items = str(value).split(",")
    return [item.strip() for item in items if item and item.strip()]


def sram_user_to_scim_body(body: dict) -> dict:
    """Rewrite a SRAM User so the generic inbound SCIM code understands it."""
    result = copy.deepcopy(body)
    sram = body.get(SRAM_USER_EXTENSION_URN) or {}
    extension = dict(result.get(WALDUR_USER_EXTENSION_URN) or {})

    affiliations = _split_affiliations(sram.get("eduPersonScopedAffiliation"))
    if affiliations:
        extension["affiliations"] = affiliations

    if "x509Certificates" in body:
        # SRAM always sends the full key list, so it is authoritative.
        extension[SSH_KEYS_ATTRIBUTE] = _decode_ssh_keys(body.get("x509Certificates"))

    if extension:
        result[WALDUR_USER_EXTENSION_URN] = extension

    name = dict(result.get("name") or {})
    if not name.get("givenName") and not name.get("familyName"):
        # SRAM users need not have given or family names, only a display name.
        given, _, family = (body.get("displayName") or "").strip().partition(" ")
        if given:
            name["givenName"] = given
            name["familyName"] = family.strip()
            result["name"] = name
    return result


def render_user(user, sram_user: models.SramUser) -> dict:
    body = waldur_to_scim_user(user, location=user_location(user))
    payload = sram_user.payload or {}
    body["externalId"] = sram_user.external_id
    if SRAM_USER_EXTENSION_URN not in body["schemas"]:
        body["schemas"].append(SRAM_USER_EXTENSION_URN)
    if payload.get("displayName"):
        body["displayName"] = payload["displayName"]
    body["x509Certificates"] = list(payload.get("x509Certificates") or [])
    body[SRAM_USER_EXTENSION_URN] = dict(payload.get(SRAM_USER_EXTENSION_URN) or {})
    return body


def group_kind(urn: str) -> str:
    parts = [part for part in (urn or "").split(":") if part]
    if len(parts) >= 3:
        return models.SramGroup.Kind.GROUP
    return models.SramGroup.Kind.COLLABORATION


def group_fields(body: dict) -> dict:
    """Model fields for a SRAM Group resource."""
    extension = body.get(SRAM_GROUP_EXTENSION_URN) or {}
    urn = (extension.get("urn") or "").strip()
    labels = extension.get("labels") or []
    if isinstance(labels, str):
        labels = [labels]
    return {
        "display_name": (body.get("displayName") or "").strip(),
        "urn": urn,
        "kind": group_kind(urn),
        "description": extension.get("description") or "",
        "labels": sorted({str(label) for label in labels if label}),
    }


def member_ids(body: dict) -> list[str]:
    ids = []
    for member in body.get("members") or []:
        if isinstance(member, dict) and member.get("value"):
            ids.append(str(member["value"]))
    return ids


def render_group(group: models.SramGroup) -> dict:
    payload = group.payload or {}
    members = []
    for user in group.members.order_by("id"):
        members.append(
            {
                "value": user.uuid.hex,
                "display": user.full_name or user.username,
                "$ref": user_location(user),
            }
        )
    body = {
        "schemas": [CORE_GROUP_URN, SRAM_GROUP_EXTENSION_URN],
        "id": group.uuid.hex,
        "externalId": group.external_id,
        "displayName": group.display_name,
        "members": members,
        SRAM_GROUP_EXTENSION_URN: dict(payload.get(SRAM_GROUP_EXTENSION_URN) or {}),
        "meta": {
            "resourceType": "Group",
            "created": group.created.isoformat(),
            "lastModified": group.modified.isoformat(),
            "location": group_location(group),
        },
    }
    return body
