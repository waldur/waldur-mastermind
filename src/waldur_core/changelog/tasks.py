import logging

from celery import shared_task
from django.apps import apps
from django.conf import settings
from django.utils import timezone

from waldur_core.changelog.models import ChangelogImpactAnalysis
from waldur_core.changelog.utils import (
    compare_versions,
    fetch_changelog_release,
    get_active_plugins,
    get_customized_settings,
    get_pending_versions,
    select_new_entries,
)
from waldur_core.core.models import User

logger = logging.getLogger(__name__)

# compute_changelog_impact() builds ORM filter() calls from "model"/"filter"
# and "filter" values in changelog entries fetched from docs.waldur.com. That
# JSON is remote, unauthenticated input: without a restriction here, a
# compromised or MITM'd response could pick any installed model and any
# filter kwargs, turning count() into a blind oracle over arbitrary data
# (e.g. narrowing User.password__startswith one guess at a time). Only these
# (model, field) combinations may be filtered on, and only with exact-match
# lookups - no "__" chains, which would otherwise allow relation traversal
# or lookups like __icontains/__startswith that widen the oracle further.
# Extending this list is a deliberate, reviewed code change, not something
# a changelog entry can request on its own.
ALLOWED_IMPACT_MODELS = {
    "marketplace.Resource": {"state"},
    "marketplace.Offering": {"state", "type"},
    "marketplace.Order": {"state", "type"},
}
ALLOWED_USER_IMPACT_FIELDS = {"is_staff", "is_support", "is_active"}


def _is_allowed_filter(filter_kwargs, allowed_fields):
    return bool(filter_kwargs) and all(
        "__" not in key and key in allowed_fields for key in filter_kwargs
    )


def _analyze_entry(entry, entry_id, active_plugins, customized_settings):
    """Build the impact-analysis result for a single changelog entry."""
    entry_result = {}

    # Count affected resources
    affected_resources = entry.get("impact", {}).get("affected_resources", {})
    model_path = affected_resources.get("model")
    model_filter = affected_resources.get("filter")
    if model_path and model_filter:
        allowed_fields = ALLOWED_IMPACT_MODELS.get(model_path)
        if allowed_fields is None or not _is_allowed_filter(
            model_filter, allowed_fields
        ):
            logger.warning(
                "Skipping affected-resources count for %s: "
                "model '%s' or filter fields %s are not allowlisted",
                entry_id,
                model_path,
                list(model_filter),
            )
        else:
            try:
                Model = apps.get_model(model_path)
                count = Model.objects.filter(**model_filter).count()
                entry_result["affected_resources_count"] = count
            except Exception as e:
                logger.warning(
                    "Failed to count affected resources for %s: %s", entry_id, e
                )

    # Count affected users
    affected_users = entry.get("impact", {}).get("affected_users", {})
    user_filter = affected_users.get("filter")
    if user_filter:
        if not _is_allowed_filter(user_filter, ALLOWED_USER_IMPACT_FIELDS):
            logger.warning(
                "Skipping affected-users count for %s: filter fields %s are not allowlisted",
                entry_id,
                list(user_filter),
            )
        else:
            try:
                count = User.objects.filter(**user_filter).count()
                entry_result["affected_users_count"] = count
            except Exception as e:
                logger.warning("Failed to count affected users for %s: %s", entry_id, e)

    # Settings diffs. relevant_when.settings names come from remote,
    # unauthenticated JSON with no allowlist, and this result is served to
    # staff *and support* via /api/changelog/pending/ - never store or
    # return the actual constance value (current_value/default_value), only
    # whether it's customized. That's all relevance matching ever needs;
    # storing the real value would leak secret_field settings (API keys,
    # passwords, ...) in plaintext to anyone who can read the endpoint.
    relevant_settings = entry.get("relevant_when", {}).get("settings", [])
    if relevant_settings:
        entry_result["settings_analysis"] = {
            setting_key: {"is_customized": setting_key in customized_settings}
            for setting_key in relevant_settings
        }

    # Plugin state
    relevant_plugins = entry.get("relevant_when", {}).get("plugins", [])
    if relevant_plugins:
        plugin_analysis = {}
        for plugin in relevant_plugins:
            plugin_analysis[plugin] = {
                "installed": plugin in active_plugins,
            }
        entry_result["plugin_analysis"] = plugin_analysis

    return entry_result


@shared_task(name="waldur_core.changelog.compute_changelog_impact")
def compute_changelog_impact(current_version, target_version):
    """Compute deployment-specific impact analysis for pending changelog entries.

    Runs as a background task to avoid blocking API responses.
    Results are stored in ChangelogImpactAnalysis model.
    """
    if not settings.WALDUR_CORE.get("CHANGELOG_ENABLED", True):
        return

    analysis, _ = ChangelogImpactAnalysis.objects.get_or_create(
        current_version=current_version,
        target_version=target_version,
        defaults={"status": ChangelogImpactAnalysis.Status.PENDING},
    )
    analysis.status = ChangelogImpactAnalysis.Status.RUNNING
    analysis.save(update_fields=["status"])

    try:
        results = {}
        pending = get_pending_versions(current_version)
        pending = [
            release_info
            for release_info in pending
            if compare_versions(release_info["version"], target_version) <= 0
        ]
        active_plugins = get_active_plugins()
        customized_settings = get_customized_settings()

        releases = []
        for release_info in pending:
            release_data = fetch_changelog_release(release_info["version"])
            if release_data:
                releases.append((release_info, release_data))

        for _release_info, _release_data, entries in select_new_entries(
            current_version, releases
        ):
            for entry in entries:
                entry_id = entry.get("id")
                if not entry_id:
                    continue

                # One malformed entry (e.g. a null "impact" from bad remote
                # data) must not sink the whole analysis and the valid
                # entries alongside it - skip and log just this one.
                try:
                    entry_result = _analyze_entry(
                        entry, entry_id, active_plugins, customized_settings
                    )
                except Exception as e:
                    logger.warning(
                        "Skipping malformed changelog entry %s: %s", entry_id, e
                    )
                    continue

                if entry_result:
                    results[entry_id] = entry_result

        analysis.results = results
        analysis.status = ChangelogImpactAnalysis.Status.COMPLETED
        analysis.computed_at = timezone.now()
        analysis.save(update_fields=["results", "status", "computed_at"])
        logger.info(
            "Changelog impact analysis completed: %s -> %s (%d entries analyzed)",
            current_version,
            target_version,
            len(results),
        )

    except Exception as e:
        logger.error(
            "Changelog impact analysis failed for %s -> %s: %s",
            current_version,
            target_version,
            e,
        )
        analysis.status = ChangelogImpactAnalysis.Status.FAILED
        analysis.error_message = str(e)
        analysis.save(update_fields=["status", "error_message"])
