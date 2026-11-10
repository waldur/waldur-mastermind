"""What loading a demo preset would do, shown before anything is deleted.

The preview describes a load that cleans up first, the default: every setting
the deployment does not own, and every feature, goes back to its default; the
preset's values are applied, then the deployment overlay's, if it sets one.
Settings the deployment owns keep their value, and a preset that sets one is
skipped -- see demo_presets/settings_reset.py.

Secret values and the passwords of staff and support users are never part of
a preview.
"""

import json
from collections import defaultdict

from waldur_core.configuration import registry
from waldur_core.configuration.models import Setting
from waldur_core.core.features import FEATURE_DEFAULTS, all_feature_keys
from waldur_core.core.models import Feature
from waldur_core.core.serializers import ConstanceSettingsSerializer
from waldur_mastermind.marketplace.demo_presets import gate, settings_reset
from waldur_mastermind.marketplace.demo_presets.manifest import DemoPresetManager

SOURCE_PRESET = "preset"
SOURCE_OVERLAY = "overlay"
SOURCE_DEFAULT = "default"
SOURCE_DEPLOYMENT = "deployment"

# What a feature or setting needs from the deployment to work. Each rule:
# (features that turn it on, settings that turn it on, settings it needs,
# what it is called). Checked against the values after the load.
REQUIREMENTS = (
    (
        ("support.enable_llm_assistant",),
        ("AI_ASSISTANT_ENABLED",),
        ("AI_ASSISTANT_API_URL", "AI_ASSISTANT_API_TOKEN"),
        "The AI assistant",
    ),
    (
        ("project.show_matrix_chat",),
        (),
        ("MATRIX_ENABLED", "MATRIX_HOMESERVER_URL", "MATRIX_APPSERVICE_AS_TOKEN"),
        "Matrix chat",
    ),
)

SCOPE_NAMES = {
    "structure.customer": "customers",
    "structure.project": "projects",
    "marketplace.offering": "offerings",
}


def _jsonable(value):
    """A value as the API returns it: dates, decimals and files as strings."""
    return json.loads(json.dumps(value, default=str))


def _same(left, right) -> bool:
    return json.dumps(_jsonable(left), sort_keys=True) == json.dumps(
        _jsonable(right), sort_keys=True
    )


def _preset_settings(data: dict) -> dict:
    return {
        key.upper(): value
        for key, value in (data.get("constance_settings") or {}).items()
    }


def _preset_features(data: dict) -> dict:
    return {
        entry["key"]: bool(entry["value"])
        for entry in data.get("features") or []
        if entry.get("key") is not None and "value" in entry
    }


def build_preview(name: str) -> dict | None:
    """The preview of the preset ``name``, or None if there is no such preset
    or it cannot be read."""
    data = DemoPresetManager.load_preset_data(name)
    if data is None:
        return None
    info = DemoPresetManager.metadata_from(name, data)

    warnings = []
    # The only caller never previews the overlay itself: it is not offered.
    overlay = gate.overlay_name()
    overlay_data = {}
    if overlay and overlay != name:
        overlay_data = DemoPresetManager.load_preset_data(overlay)
        if overlay_data is None:
            state = (
                "cannot be read"
                if DemoPresetManager.get_preset_path(overlay)
                else "is missing"
            )
            warnings.append(
                f"The deployment's overlay preset '{overlay}' {state}, so a "
                "load would fail before deleting anything."
            )
            overlay_data = {}
        else:
            overlay_data["_name"] = overlay

    settings_changes, after = _settings_changes(data, overlay_data, warnings)
    feature_changes, features_after = _feature_changes(data, overlay_data, warnings)
    warnings.extend(_missing_requirements(after, features_after))

    return {
        "name": info.name,
        "title": info.title,
        "description": info.description,
        "version": info.version,
        "entity_counts": info.entity_counts,
        "scenarios": info.scenarios,
        "overlay": overlay_data.get("_name"),
        "users": _users(data),
        "organizations": _organizations(data),
        "offerings": _offerings(data),
        "overlay_contents": {
            # The overlay's accounts are the deployment's own, such as its
            # agents: their passwords are never shown.
            "users": _users(overlay_data, passwords=False),
            "organizations": _organizations(overlay_data),
            "offerings": _offerings(overlay_data),
        }
        if overlay_data
        else None,
        "settings_changes": settings_changes,
        "feature_changes": feature_changes,
        "warnings": warnings,
    }


def _users(data: dict, passwords=True) -> list[dict]:
    names = {
        scope_type: {
            item.get("uuid"): item.get("name") for item in data.get(collection) or []
        }
        for scope_type, collection in SCOPE_NAMES.items()
    }
    roles = defaultdict(list)
    for role in data.get("user_roles") or []:
        if role.get("is_active") is False:
            continue
        scope_type = role.get("scope_type", "")
        roles[role.get("user_uuid")].append(
            {
                "role": role.get("role_name", ""),
                "scope_type": scope_type,
                "scope_name": names.get(scope_type, {}).get(role.get("scope_uuid"))
                or "",
            }
        )
    users = []
    for user in data.get("users") or []:
        is_staff = bool(user.get("is_staff"))
        is_support = bool(user.get("is_support"))
        is_active = user.get("is_active", True) is not False
        users.append(
            {
                "username": user.get("username", ""),
                "email": user.get("email", ""),
                "full_name": " ".join(
                    part
                    for part in (user.get("first_name"), user.get("last_name"))
                    if part
                ),
                "is_staff": is_staff,
                "is_support": is_support,
                "is_active": is_active,
                # A staff or support password would give anyone who reads the
                # preview portal-wide access on a public demo; an inactive
                # user cannot log in with one.
                "password": user.get("password") or None
                if passwords and is_active and not (is_staff or is_support)
                else None,
                "roles": roles.get(user.get("uuid"), []),
            }
        )
    return users


def _organizations(data: dict) -> list[dict]:
    resources = defaultdict(list)
    for resource in data.get("resources") or []:
        resources[resource.get("project_uuid")].append(resource.get("name", ""))
    projects = defaultdict(list)
    for project in data.get("projects") or []:
        projects[project.get("customer_uuid")].append(
            {
                "name": project.get("name", ""),
                "resources": sorted(resources.get(project.get("uuid"), [])),
            }
        )
    return [
        {
            "name": customer.get("name", ""),
            "projects": sorted(
                projects.get(customer.get("uuid"), []), key=lambda p: p["name"]
            ),
        }
        for customer in sorted(
            data.get("customers") or [], key=lambda c: c.get("name", "")
        )
    ]


def _offerings(data: dict) -> list[dict]:
    categories = {
        category.get("uuid"): category.get("title", "")
        for category in data.get("categories") or []
    }
    groups = defaultdict(list)
    for offering in data.get("offerings") or []:
        key = (
            offering.get("type", ""),
            categories.get(offering.get("category_uuid"), ""),
        )
        groups[key].append(offering.get("name", ""))
    return [
        {"type": type_, "category": category, "names": sorted(names)}
        for (type_, category), names in sorted(groups.items())
    ]


def _settings_changes(data, overlay_data, warnings):
    """The settings a cleanup load changes or the presets set, and the value
    of every setting after the load."""
    current = Setting.objects.values_with_defaults()
    owned = settings_reset.owned_keys()
    from_preset = _preset_settings(data)
    from_overlay = _preset_settings(overlay_data)

    for key in sorted((from_preset.keys() | from_overlay.keys()) - current.keys()):
        warnings.append(f"Unknown setting {key} would be ignored.")
    for label, values in (("preset", from_preset), ("overlay", from_overlay)):
        _drop_rejected(label, values, owned, warnings)

    after = {}
    changes = []
    for key in sorted(current):
        if key in from_overlay:
            source, value = SOURCE_OVERLAY, from_overlay[key]
        elif key in from_preset:
            source, value = SOURCE_PRESET, from_preset[key]
        else:
            source, value = SOURCE_DEFAULT, registry.default(key)
        skipped = key in owned
        if skipped:
            # The deployment's value stays; a preset setting it is skipped.
            after[key] = current[key]
            if source == SOURCE_DEFAULT:
                continue
            source = SOURCE_DEPLOYMENT
        else:
            after[key] = value
            if source == SOURCE_DEFAULT and _same(current[key], value):
                continue
        secret = registry.is_secret(key)
        changes.append(
            {
                "key": key,
                "current_value": None if secret else _jsonable(current[key]),
                "new_value": None if secret else _jsonable(after[key]),
                "source": source,
                "changed": not _same(current[key], after[key]),
                "skipped": skipped,
                "secret": secret,
            }
        )
    return changes, after


def _drop_rejected(label, values, owned, warnings):
    """Drop the values the import would reject, as it does: the setting then
    keeps the default the load returned it to."""
    checked = {
        key: value
        for key, value in values.items()
        if registry.has(key) and key not in owned
    }
    if not checked:
        return
    serializer = ConstanceSettingsSerializer(data=checked)
    if serializer.is_valid():
        return
    for key, errors in sorted(serializer.errors.items()):
        if key not in values:
            continue
        del values[key]
        reason = errors[0] if isinstance(errors, list) and errors else errors
        warnings.append(
            f"The {label} sets {key} to a value the load would reject ({reason}); "
            "it keeps its default."
        )


def _feature_changes(data, overlay_data, warnings):
    """The features a cleanup load changes or the presets set, and the value
    of every feature after the load."""
    keys = all_feature_keys()
    stored = dict(Feature.objects.values_list("key", "value"))
    from_preset = _preset_features(data)
    from_overlay = _preset_features(overlay_data)

    for key in sorted((from_preset.keys() | from_overlay.keys()) - set(keys)):
        warnings.append(f"Unknown feature {key} would be ignored.")

    after = {}
    changes = []
    for key in sorted(keys):
        default = FEATURE_DEFAULTS.get(key, False)
        current = stored.get(key, default)
        if key in from_overlay:
            source, value = SOURCE_OVERLAY, from_overlay[key]
        elif key in from_preset:
            source, value = SOURCE_PRESET, from_preset[key]
        else:
            source, value = SOURCE_DEFAULT, default
        after[key] = value
        if source == SOURCE_DEFAULT and current == value:
            continue
        changes.append(
            {
                "key": key,
                "current_value": current,
                "new_value": value,
                "source": source,
                "changed": current != value,
            }
        )
    return changes, after


def _missing_requirements(settings_after, features_after):
    warnings = []
    for features, switches, needs, title in REQUIREMENTS:
        on = any(features_after.get(key) for key in features) or any(
            settings_after.get(key) for key in switches
        )
        if not on:
            continue
        missing = [key for key in needs if not settings_after.get(key)]
        if missing:
            warnings.append(
                f"{title} is turned on, but after the load "
                f"{', '.join(missing)} would be unset."
            )
    return warnings
