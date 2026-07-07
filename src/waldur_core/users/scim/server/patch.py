"""SCIM 2.0 PATCH operation processor (RFC 7644 §3.5.2).

Reduces an incoming PATCH operation set against a SCIM resource representation
into:

- a flat attribute payload to apply via the normal resource-update path
- ``active``-toggle requests (for Users)
- ``members`` add / remove deltas (for Groups)

The implementation supports the pragmatic subset emitted by Okta, Microsoft
Entra ID and Keycloak:

- ``op: replace`` with no ``path`` and value as a full attribute dict
- ``op: replace`` / ``op: add`` / ``op: remove`` on top-level attributes
- ``op: add`` / ``op: remove`` / ``op: replace`` on ``members`` with the value-
  filter form ``members[value eq "<uuid>"]``

Anything outside this subset returns 400 ``invalidPath`` rather than silently
mis-applying — SCIM clients then fail loudly which is the desired behaviour.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from waldur_core.users.scim.server.exceptions import ScimError

PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"

# members[value eq "<id>"]
_MEMBER_FILTER_RE = re.compile(
    r'^members\[\s*value\s+eq\s+"([^"]+)"\s*\]$', re.IGNORECASE
)

_ENTERPRISE_URN = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:user"
_WALDUR_URN = "urn:waldur:params:scim:schemas:extension:user:1.0"

# sshPublicKeys[value eq "<key material>"], optionally URN-prefixed (Entra ID).
# The capture group preserves case — key material is case-sensitive base64.
_SSH_FILTER_RE = re.compile(
    r"^(?:urn:waldur:params:scim:schemas:extension:user:1\.0:)?"
    r'sshpublickeys\[\s*value\s+eq\s+"([^"]+)"\s*\]$',
    re.IGNORECASE,
)
_SSH_KEYS_PATHS = {"sshpublickeys", f"{_WALDUR_URN}:sshpublickeys"}

# URN-prefixed attribute paths (Entra ID sends these for extension fields):
# path → (waldur field, empty value used by 'remove').
# civil_number clears to None, not "" — it carries a unique constraint and
# relies on NULL for absent values.
_EXTENSION_ATTRIBUTE_PATHS = {
    f"{_ENTERPRISE_URN}:organization": ("organization", ""),
    f"{_WALDUR_URN}:civilnumber": ("civil_number", None),
    f"{_WALDUR_URN}:affiliations": ("affiliations", []),
    f"{_WALDUR_URN}:edupersonassurance": ("eduperson_assurance", []),
}

# Whole-extension paths: value is the extension object.
_EXTENSION_OBJECT_PATHS = {
    _ENTERPRISE_URN: {"organization": ("organization", "")},
    _WALDUR_URN: {
        "civilnumber": ("civil_number", None),
        "affiliations": ("affiliations", []),
        "edupersonassurance": ("eduperson_assurance", []),
    },
}


@dataclass
class UserPatchResult:
    """Outcome of applying a PATCH operation set against a User."""

    attributes: dict = field(default_factory=dict)
    set_active: bool | None = None
    set_external_id: str | None = None
    # SSH keys are a related collection, applied separately from `attributes`.
    # Entries are raw SCIM dicts ({"value", "display"}); the view normalises them.
    add_ssh_keys: list[dict] = field(default_factory=list)
    remove_ssh_key_values: list[str] = field(default_factory=list)
    replace_ssh_keys: list[dict] | None = None  # None = no full replace requested


@dataclass
class GroupPatchResult:
    """Outcome of applying a PATCH operation set against a Group."""

    add_member_ids: list[str] = field(default_factory=list)
    remove_member_ids: list[str] = field(default_factory=list)
    replace_member_ids: list[str] | None = None  # None = no full replace requested


def validate_patch_envelope(body: dict) -> list[dict]:
    """Validate the PATCH envelope and return the Operations list."""
    schemas = body.get("schemas") or []
    if PATCH_SCHEMA not in schemas:
        raise ScimError(
            400,
            f"PATCH body missing schema {PATCH_SCHEMA!r}.",
            scim_type="invalidSyntax",
        )
    operations = body.get("Operations") or body.get("operations")
    if not isinstance(operations, list) or not operations:
        raise ScimError(
            400,
            "PATCH 'Operations' must be a non-empty list.",
            scim_type="invalidSyntax",
        )
    return operations


def apply_user_patch(operations: list[dict]) -> UserPatchResult:
    """Reduce a PATCH operation list against a SCIM User."""
    result = UserPatchResult()
    for op in operations:
        verb = (op.get("op") or "").lower()
        path = op.get("path")
        value = op.get("value")

        if verb not in {"add", "replace", "remove"}:
            raise ScimError(
                400, f"Unsupported PATCH op {verb!r}.", scim_type="invalidValue"
            )

        if not path:
            # No-path: value is a flat dict of attributes to replace.
            if verb == "remove":
                raise ScimError(400, "'remove' requires a path.", scim_type="noTarget")
            if not isinstance(value, dict):
                raise ScimError(
                    400,
                    "PATCH replace without path requires an object value.",
                    scim_type="invalidValue",
                )
            _merge_user_attributes(result, value)
            continue

        _apply_user_path(result, verb, path, value)
    return result


def apply_group_patch(operations: list[dict]) -> GroupPatchResult:
    """Reduce a PATCH operation list against a SCIM Group."""
    result = GroupPatchResult()
    for op in operations:
        verb = (op.get("op") or "").lower()
        path = op.get("path") or ""
        value = op.get("value")
        path_lc = path.lower()

        if verb not in {"add", "replace", "remove"}:
            raise ScimError(
                400, f"Unsupported PATCH op {verb!r}.", scim_type="invalidValue"
            )

        if path_lc == "members":
            ids = _extract_member_ids(value)
            if verb == "add":
                result.add_member_ids.extend(ids)
            elif verb == "remove":
                if ids:
                    result.remove_member_ids.extend(ids)
                else:
                    # remove all members
                    result.replace_member_ids = []
            else:  # replace
                result.replace_member_ids = list(ids)
            continue

        member_filter = _MEMBER_FILTER_RE.match(path)
        if member_filter:
            member_id = member_filter.group(1)
            if verb == "remove":
                result.remove_member_ids.append(member_id)
            elif verb == "add":
                result.add_member_ids.append(member_id)
            else:
                raise ScimError(
                    400,
                    "PATCH 'replace' on a filtered member is not supported.",
                    scim_type="invalidPath",
                )
            continue

        if not path:
            if verb == "remove":
                raise ScimError(400, "'remove' requires a path.", scim_type="noTarget")
            if not isinstance(value, dict):
                raise ScimError(
                    400,
                    "PATCH replace without path requires an object value.",
                    scim_type="invalidValue",
                )
            if "members" in value:
                result.replace_member_ids = list(_extract_member_ids(value["members"]))
            continue

        raise ScimError(
            400, f"Unsupported PATCH path {path!r}.", scim_type="invalidPath"
        )

    return result


def _apply_user_path(result: UserPatchResult, verb: str, path: str, value) -> None:
    """Handle a single User PATCH op with an explicit path."""
    lc = path.lower()
    if lc == "active":
        if verb == "remove":
            raise ScimError(400, "Cannot remove 'active'.", scim_type="invalidPath")
        if isinstance(value, str):
            value = value.lower() == "true"
        result.set_active = bool(value)
        return
    if lc == "externalid":
        if verb == "remove":
            result.set_external_id = ""
        else:
            result.set_external_id = str(value) if value is not None else ""
        return
    if lc == "username":
        # userName changes are rejected at the view layer — surface a 400 here
        # if a PATCH attempts it, to keep validation consistent.
        raise ScimError(
            400,
            "Changing 'userName' via PATCH is not allowed.",
            scim_type="mutability",
        )

    if lc == "name":
        # Whole-name ops: value is a SCIM name object.
        if verb == "remove":
            result.attributes["first_name"] = ""
            result.attributes["last_name"] = ""
            return
        if not isinstance(value, dict):
            raise ScimError(
                400,
                "PATCH on 'name' requires an object value.",
                scim_type="invalidValue",
            )
        if "givenName" in value:
            result.attributes["first_name"] = value.get("givenName") or ""
        if "familyName" in value:
            result.attributes["last_name"] = value.get("familyName") or ""
        return

    mapping = {
        "name.givenname": "first_name",
        "name.familyname": "last_name",
        "displayname": None,  # ignored — derived
    }
    if lc in mapping:
        target = mapping[lc]
        if target is None:
            return
        if verb == "remove":
            value = ""
        result.attributes[target] = value if value is not None else ""
        return

    if lc == "emails":
        if verb == "remove":
            result.attributes["email"] = ""
        else:
            email = _first_email_value(value)
            if email is not None:
                result.attributes["email"] = email
        return

    if lc == "phonenumbers":
        if verb == "remove":
            result.attributes["phone_number"] = ""
        else:
            phone = _first_email_value(value)  # same shape (value/primary)
            if phone is not None:
                result.attributes["phone_number"] = phone
        return

    ssh_filter = _SSH_FILTER_RE.match(path)
    if ssh_filter:
        material = ssh_filter.group(1)
        if verb == "remove":
            result.remove_ssh_key_values.append(material)
        elif verb == "add":
            result.add_ssh_keys.append({"value": material})
        else:
            raise ScimError(
                400,
                "PATCH 'replace' on a filtered sshPublicKeys entry is not supported.",
                scim_type="invalidPath",
            )
        return

    if lc in _SSH_KEYS_PATHS:
        if verb == "add":
            result.add_ssh_keys.extend(_ssh_key_entries(value))
        elif verb == "remove":
            result.replace_ssh_keys = []  # remove all managed keys
        else:  # replace
            result.replace_ssh_keys = _ssh_key_entries(value)
        return

    if lc in _EXTENSION_ATTRIBUTE_PATHS:
        target, empty = _EXTENSION_ATTRIBUTE_PATHS[lc]
        if verb == "remove" or value is None or value == "":
            result.attributes[target] = empty
        else:
            result.attributes[target] = value
        return

    if lc in _EXTENSION_OBJECT_PATHS:
        fields = _EXTENSION_OBJECT_PATHS[lc]
        if verb == "remove":
            for target, empty in fields.values():
                result.attributes[target] = empty
            return
        if not isinstance(value, dict):
            raise ScimError(
                400,
                "PATCH on an extension schema requires an object value.",
                scim_type="invalidValue",
            )
        for key, item in value.items():
            if key.lower() == "sshpublickeys":
                # Whole Waldur extension replaced -> keys are authoritative.
                result.replace_ssh_keys = _ssh_key_entries(item)
                continue
            entry = fields.get(key.lower())
            if entry:
                target, empty = entry
                result.attributes[target] = item if item is not None else empty
        return

    raise ScimError(400, f"Unsupported PATCH path {path!r}.", scim_type="invalidPath")


def _ssh_key_entries(value) -> list[dict]:
    """Coerce a PATCH value into a list of raw SCIM sshPublicKeys entries."""
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return value
    raise ScimError(
        400,
        "'sshPublicKeys' value must be an array of key objects.",
        scim_type="invalidValue",
    )


def _merge_user_attributes(result: UserPatchResult, value: dict) -> None:
    """Apply a no-path replace value (a SCIM User dict subset)."""
    from waldur_core.users.scim.server.mapping import scim_to_waldur_payload

    flat = scim_to_waldur_payload(value)
    result.attributes.update(flat)

    if "active" in value:
        active = value["active"]
        if isinstance(active, str):
            active = active.lower() == "true"
        result.set_active = bool(active)
    if "externalId" in value:
        result.set_external_id = str(value["externalId"] or "")

    # A no-path replace is a whole-resource replace (PUT semantics): if the
    # Waldur extension carries sshPublicKeys, treat it as authoritative.
    for key, item in value.items():
        if key.lower() == _WALDUR_URN and isinstance(item, dict):
            for sub_key, sub_val in item.items():
                if sub_key.lower() == "sshpublickeys":
                    result.replace_ssh_keys = _ssh_key_entries(sub_val)


def _first_email_value(value) -> str | None:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and item.get("primary") and item.get("value"):
                return item["value"]
        for item in value:
            if isinstance(item, dict) and item.get("value"):
                return item["value"]
        return None
    if isinstance(value, dict):
        return value.get("value")
    if isinstance(value, str):
        return value
    return None


def _extract_member_ids(value) -> list[str]:
    """``[{"value": "<uuid>"}, ...]`` → ``["<uuid>", ...]``."""
    if value is None:
        return []
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        raise ScimError(
            400,
            "Group 'members' value must be an array of member objects.",
            scim_type="invalidValue",
        )
    ids: list[str] = []
    for item in value:
        if isinstance(item, dict) and item.get("value"):
            ids.append(str(item["value"]))
        elif isinstance(item, str):
            ids.append(item)
        else:
            raise ScimError(
                400,
                f"Invalid member entry {item!r}.",
                scim_type="invalidValue",
            )
    return ids
