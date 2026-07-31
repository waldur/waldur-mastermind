"""Per-deployment Sentry Crons monitors with usable failure tolerances.

sentry-sdk's ``CeleryIntegration(monitor_beat_tasks=True)`` derives the monitor
slug from the celery beat *schedule entry name*. Those names are identical in
every Waldur installation, so every deployment reporting to the same Sentry
project lands on one shared monitor, distinguished only by the environment tag.

Sentry then expects a check-in from every environment that ever checked in, for
as long as the monitor lives. A deployment that stops running an entry - because
the plugin is disabled there, or beat was restarted, or it is on a release where
the entry does not exist - produces a missed check-in every scheduling window
forever, while the monitor stays alive because other deployments keep checking
in. One shared monitor therefore fans a single condition out into an issue per
environment.

``scope_beat_schedule`` gives each deployment its own monitor by suffixing the
entry names with the environment. ``patch_monitor_config_defaults`` adds the
tolerance fields that sentry-sdk never sets, so a single late check-in no longer
opens an issue.

This module is imported from the deployment settings file, so it must not import
Django models or anything that needs the app registry.
"""

import hashlib
import re

# Sentry rejects monitor slugs longer than this.
MONITOR_SLUG_MAX_LENGTH = 50

# Length of the disambiguating hash appended when a scoped name would overflow.
_HASH_LENGTH = 6

# Below this, the truncated entry name carries too little meaning to be worth
# keeping the scope readable at its expense.
_MIN_NAME_CHARS = 12

_SLUG_CLEANUP = re.compile(r"[^a-zA-Z0-9_-]+")


def _slugify(value):
    """Reduce an arbitrary environment name to slug-safe characters."""
    return _SLUG_CLEANUP.sub("-", value).strip("-").lower()


def scope_monitor_name(name, scope):
    """Return a monitor name unique to this deployment.

    When the combination would exceed what Sentry accepts, the *entry name* is
    truncated and a hash of the full scoped name is inserted to keep it unique -
    the scope is preserved verbatim, so a monitor is still identifiable as
    belonging to this deployment when read from the Sentry Crons list.
    """
    if not scope:
        return name

    scope = _slugify(scope)
    if not scope:
        return name

    scoped = f"{name}-{scope}"
    if len(scoped) <= MONITOR_SLUG_MAX_LENGTH:
        return scoped

    digest = hashlib.sha256(scoped.encode("utf-8")).hexdigest()[:_HASH_LENGTH]
    # Two separators, plus room for the hash and the whole scope.
    keep = MONITOR_SLUG_MAX_LENGTH - len(digest) - len(scope) - 2

    if keep < _MIN_NAME_CHARS:
        # The scope alone crowds out the entry name; identifying the deployment
        # matters less than telling two monitors apart, so fall back to hashing.
        keep = MONITOR_SLUG_MAX_LENGTH - len(digest) - 1
        return f"{scoped[:keep]}-{digest}"

    return f"{name[:keep]}-{digest}-{scope}"


def scope_beat_schedule(beat_schedule, scope):
    """Rename beat entries so each deployment gets its own Sentry monitor.

    Returns a new dict; the original is left untouched. Entry names are only
    used as identifiers (by beat's persistent state and by Sentry Crons), not to
    look tasks up, so renaming is safe. Beat's last-run state is keyed by name,
    so entries fire once shortly after this first takes effect.
    """
    if not scope:
        return dict(beat_schedule)

    scoped = {}
    for name, config in beat_schedule.items():
        scoped_name = scope_monitor_name(name, scope)
        if scoped_name in scoped:
            # Should not happen given the hash suffix, but never silently drop a
            # scheduled task: keep the original name so the entry still runs.
            scoped[name] = config
            continue
        scoped[scoped_name] = config
    return scoped


def build_monitor_config_defaults(
    checkin_margin=None,
    failure_issue_threshold=None,
    recovery_threshold=None,
    max_runtime=None,
):
    """Collect the tolerance fields sentry-sdk leaves unset, dropping blanks."""
    defaults = {
        "checkin_margin": checkin_margin,
        "failure_issue_threshold": failure_issue_threshold,
        "recovery_threshold": recovery_threshold,
        "max_runtime": max_runtime,
    }
    return {key: value for key, value in defaults.items() if value is not None}


def apply_monitor_config_defaults(monitor_config, defaults):
    """Merge tolerance defaults into a monitor config built by sentry-sdk.

    An empty config means sentry-sdk could not express the schedule and will
    skip the monitor entirely; leave it that way.
    """
    if not monitor_config:
        return monitor_config

    for key, value in defaults.items():
        monitor_config.setdefault(key, value)
    return monitor_config


def patch_monitor_config_defaults(defaults):
    """Wrap sentry-sdk's monitor config builder so every monitor gets tolerances.

    sentry-sdk offers no hook for this - ``_get_monitor_config`` builds the dict
    from the celery schedule alone, and the result is upserted on every beat
    tick, which would overwrite anything configured in the Sentry UI. Wrapping
    the builder is the only way to make the setting stick.

    Returns True when the patch was applied, False when the sentry-sdk internals
    have moved and the caller should carry on without it.
    """
    if not defaults:
        return False

    try:
        from sentry_sdk.integrations.celery import beat as sentry_beat
    except ImportError:
        return False

    original = getattr(sentry_beat, "_get_monitor_config", None)
    if original is None:
        return False

    if getattr(original, "_waldur_patched", False):
        return True

    def _get_monitor_config_with_defaults(celery_schedule, app, monitor_name):
        return apply_monitor_config_defaults(
            original(celery_schedule, app, monitor_name), defaults
        )

    _get_monitor_config_with_defaults._waldur_patched = True
    sentry_beat._get_monitor_config = _get_monitor_config_with_defaults
    return True
