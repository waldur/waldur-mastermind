"""SCIM-managed SSH public keys for the inbound Service Provider.

SSH keys are a *related collection* (``User`` → many ``SshPublicKey`` rows), so
they cannot flow through ``update_user_attributes_from_source`` like the flat
User attributes do. This module owns the diff/sync logic invoked from the
``/Users`` views.

Representation: a multi-valued complex attribute ``sshPublicKeys`` under the
Waldur User extension URN, mirroring the SCIM ``emails`` shape::

    { "value": "<public key material>", "display": "<name>", "primary": false }

Ownership policy (``SCIM_INBOUND_SSH_KEYS_ENABLED``): SCIM is authoritative. A
full replace (POST create / PUT / PATCH replace) makes the incoming set the
complete set of the user's keys — keys absent from the payload are deleted,
including ones the user added via the UI.

Keys are matched by their *material identity* (algorithm + base64 body), so the
optional trailing comment and surrounding whitespace never cause spurious
recreation on re-sync.
"""

from __future__ import annotations

from typing import Any

from constance import config
from django.core.exceptions import ValidationError

from waldur_core.core.models import SshPublicKey
from waldur_core.core.validators import validate_ssh_public_key
from waldur_core.users.scim.server.exceptions import ScimError

SSH_KEYS_ATTRIBUTE = "sshPublicKeys"


def parse_ssh_key_entries(extension: dict) -> list[dict] | None:
    """Normalise the ``sshPublicKeys`` extension attribute.

    Returns:
    - ``None`` when the attribute is absent — callers must leave keys untouched.
    - ``[]`` when present but empty — a request to clear all managed keys.
    - a list of ``{"public_key", "name"}`` otherwise.

    Raises ``ScimError(400)`` on malformed entries (missing ``value``).
    """
    if SSH_KEYS_ATTRIBUTE not in extension:
        return None
    return normalize_entries(extension.get(SSH_KEYS_ATTRIBUTE))


def normalize_entries(value: Any) -> list[dict]:
    """``[{"value": ..., "display": ...}, ...]`` → ``[{"public_key", "name"}]``."""
    if value is None:
        return []
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        raise ScimError(
            400,
            "'sshPublicKeys' value must be an array of key objects.",
            scim_type="invalidValue",
        )
    entries: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            raise ScimError(
                400,
                f"Invalid sshPublicKeys entry {item!r}.",
                scim_type="invalidValue",
            )
        public_key = (item.get("value") or "").strip()
        if not public_key:
            raise ScimError(
                400,
                "Each sshPublicKeys entry requires a non-empty 'value'.",
                scim_type="invalidValue",
            )
        name = (item.get("display") or "").strip()
        entries.append({"public_key": public_key, "name": name})
    return entries


def _validate_key_material(public_key: str) -> None:
    """Mirror ``SshKeySerializer.validate_public_key`` as SCIM errors.

    Replicated (not imported) to avoid coupling to the DRF request context.
    """
    if len(public_key.splitlines()) > 1:
        raise ScimError(
            400,
            "SSH public key is not valid: it should be a single line.",
            scim_type="invalidValue",
        )
    try:
        validate_ssh_public_key(public_key)
    except ValidationError as exc:
        raise ScimError(
            400,
            f"SSH public key is not valid: {'; '.join(exc.messages)}",
            scim_type="invalidValue",
        )
    allowed_types = config.SSH_KEY_ALLOWED_TYPES
    key_type = public_key.split()[0]
    if allowed_types and key_type not in allowed_types:
        raise ScimError(
            400,
            f"SSH key type {key_type!r} is not allowed. "
            f"Allowed types: {', '.join(allowed_types)}.",
            scim_type="invalidValue",
        )


def _key_identity(public_key: str) -> str:
    """Algorithm + base64 body — the comment-independent identity of a key."""
    parts = public_key.split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[1]}"
    return public_key.strip()


def _resolve_name(desired: str, identity: str, taken: set[str]) -> str:
    """Pick a per-user-unique name, honouring ``unique_together=(user, name)``.

    Blank ``display`` derives a stable name from the key body so multiple
    unnamed keys don't collide on the empty string.
    """
    base = desired.strip()
    if not base:
        body = identity.split(" ")[-1]
        base = f"scim-{body[:12]}"
    name = base
    counter = 2
    while name in taken:
        name = f"{base} ({counter})"
        counter += 1
    return name


def sync_ssh_keys(user, entries: list[dict], *, replace: bool) -> None:
    """Reconcile SCIM-managed SSH keys against the user's ``SshPublicKey`` rows.

    ``replace=True`` (POST/PUT/PATCH-replace): the incoming set is authoritative
    — create missing keys and delete any existing key absent from it.
    ``replace=False`` (PATCH-add): only add the incoming keys; never delete.

    Runs inside the caller's ``transaction.atomic()``.
    """
    for entry in entries:
        _validate_key_material(entry["public_key"])

    existing = list(SshPublicKey.objects.filter(user=user))
    existing_by_identity = {_key_identity(k.public_key): k for k in existing}
    incoming_identities: set[str] = set()
    taken_names = {k.name for k in existing}

    for entry in entries:
        identity = _key_identity(entry["public_key"])
        if identity in incoming_identities:
            # Duplicate key within the same payload — first occurrence wins.
            continue
        incoming_identities.add(identity)
        if identity in existing_by_identity:
            continue
        name = _resolve_name(entry["name"], identity, taken_names)
        taken_names.add(name)
        key = SshPublicKey(user=user, name=name, public_key=entry["public_key"])
        _save_key(key)

    if replace:
        for identity, key in existing_by_identity.items():
            if identity not in incoming_identities:
                key.delete()


def remove_ssh_keys(user, match_values: list[str]) -> None:
    """Remove keys whose material matches any of ``match_values``.

    Matches on key identity (algorithm + body), so a supplied ``value`` with a
    different comment still removes the intended key.
    """
    if not match_values:
        return
    wanted = {_key_identity(v) for v in match_values if v}
    for key in SshPublicKey.objects.filter(user=user):
        if _key_identity(key.public_key) in wanted:
            key.delete()


def remove_all_ssh_keys(user) -> None:
    SshPublicKey.objects.filter(user=user).delete()


def _save_key(key: SshPublicKey) -> None:
    """Persist a key, translating model-level failures into SCIM 400s."""
    try:
        key.save()
    except ValueError as exc:
        # Raised by SshPublicKey.save() when fingerprint calculation fails.
        raise ScimError(400, str(exc), scim_type="invalidValue")


def serialize_ssh_keys(user) -> list[dict]:
    """Render a user's SSH keys as SCIM multi-valued attribute entries."""
    keys = SshPublicKey.objects.filter(user=user).order_by("name", "id")
    return [
        {"value": key.public_key, "display": key.name, "primary": False} for key in keys
    ]
