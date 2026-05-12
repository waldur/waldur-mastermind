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


@dataclass
class UserPatchResult:
    """Outcome of applying a PATCH operation set against a User."""

    attributes: dict = field(default_factory=dict)
    set_active: bool | None = None
    set_external_id: str | None = None


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

    raise ScimError(400, f"Unsupported PATCH path {path!r}.", scim_type="invalidPath")


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
