"""Whether this deployment offers demo presets through the API.

Loading a preset deletes every organization, project, offering, user and token,
so the API is closed unless the deployment opts in with
WALDUR_CORE["DEMO_PRESETS_ENABLED"]. That setting comes from the environment
only; it is not a Constance setting, so it cannot be switched on from the
portal. The `waldur demo_presets` command is not gated: shell access to the
deployment already implies database access.
"""

from django.conf import settings


def is_enabled() -> bool:
    return bool(settings.WALDUR_CORE.get("DEMO_PRESETS_ENABLED"))


def overlay_name() -> str:
    """The preset the deployment imports after every cleanup load, if any."""
    return settings.WALDUR_CORE.get("DEMO_PRESETS_OVERLAY") or ""


def staff_password() -> str:
    """The password the deployment gives preset staff users, if any."""
    return settings.WALDUR_CORE.get("DEMO_PRESETS_STAFF_PASSWORD") or ""


def is_allowed(name: str) -> bool:
    """Whether the preset `name` is offered. An empty allowlist offers all.

    The deployment's overlay never is: it is applied after every load, not
    loaded in place of a demo."""
    if not is_enabled() or name == overlay_name():
        return False
    allowed = settings.WALDUR_CORE.get("DEMO_PRESETS_ALLOWED") or []
    return not allowed or name in allowed
