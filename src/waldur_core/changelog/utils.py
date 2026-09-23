import logging

import requests
from constance import config
from django.conf import settings
from django.core.cache import cache
from packaging.version import InvalidVersion, Version

from waldur_core.core.models import Feature

logger = logging.getLogger(__name__)

CHANGELOG_INDEX_CACHE_KEY = "waldur_changelog_index"
CHANGELOG_RELEASE_CACHE_PREFIX = "waldur_changelog_release_"
CHANGELOG_BASE_URL = "https://docs.waldur.com/latest/changelog"
CACHE_TIMEOUT_INDEX = 3600  # 1 hour
CACHE_TIMEOUT_RELEASE = 86400 * 30  # 30 days (static per version)
# Short-lived negative cache for failed fetches, so an outage at
# docs.waldur.com doesn't turn into one outbound HTTP call per incoming
# request. cache.get() returns None for both "never cached" and "cached
# value is None", so a distinct sentinel (compared by equality, not
# identity, since a distributed cache backend deserializes it into a new
# object) marks a recently-failed fetch instead of reusing None for that.
CACHE_TIMEOUT_NEGATIVE = 60  # 1 minute
_FETCH_FAILED = "__failed__"


def is_changelog_enabled():
    return settings.WALDUR_CORE.get("CHANGELOG_ENABLED", True)


def parse_version(version_string):
    """Parse a version string with RC support.

    Handles both PEP 440 and SemVer RC formats:
    - '8.0.7' → Version('8.0.7')
    - '8.0.7-rc.5' → Version('8.0.7rc5')
    """
    # Convert SemVer RC format to PEP 440
    normalized = version_string.replace("-rc.", "rc")
    try:
        return Version(normalized)
    except InvalidVersion:
        return None


def compare_versions(a, b):
    """Compare two version strings. Returns negative if a < b, 0 if equal, positive if a > b."""
    va = parse_version(a)
    vb = parse_version(b)
    if va is None or vb is None:
        return 0
    if va < vb:
        return -1
    if va > vb:
        return 1
    return 0


def is_rc_version(version_string):
    """Whether version_string is a release candidate (or a build of one)."""
    parsed = parse_version(version_string)
    return bool(parsed and parsed.is_prerelease)


def is_same_release(a, b):
    """Whether two version strings name the same release. A development or
    staging build reports its release plus a local build suffix
    (8.1.3-rc.16+5.gb6fab9572), which is still that release."""
    parsed_a, parsed_b = parse_version(a or ""), parse_version(b or "")
    if parsed_a is None or parsed_b is None:
        return a == b
    return parsed_a.public == parsed_b.public


def get_latest_version(index_data, current_version):
    """The newest release this deployment should be offered.

    A stable deployment is only pointed at stable releases. A deployment
    running an RC already tracks release candidates, so a newer RC counts too.
    """
    latest_stable = index_data.get("latest_stable")
    if not is_rc_version(current_version):
        return latest_stable
    candidates = [
        version
        for version in (latest_stable, index_data.get("latest_rc"))
        if version and parse_version(version)
    ]
    return max(candidates, key=parse_version, default=None)


def count_versions_behind(current_version, pending):
    """Pending releases counted the way get_latest_version() offers them:
    every newer release for an RC deployment, stable ones otherwise."""
    if is_rc_version(current_version):
        return len(pending)
    return sum(1 for release in pending if release.get("type") == "stable")


def _fetch_json(url, timeout, cache_key, success_timeout):
    """Fetch JSON from docs.waldur.com, caching both success and failure."""
    cached = cache.get(cache_key)
    if cached == _FETCH_FAILED:
        return None
    if cached is not None:
        return cached

    try:
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError, requests.JSONDecodeError) as e:
        logger.error("Failed to fetch %s: %s", url, e)
        cache.set(cache_key, _FETCH_FAILED, timeout=CACHE_TIMEOUT_NEGATIVE)
        return None

    cache.set(cache_key, data, timeout=success_timeout)
    return data


def fetch_changelog_index(timeout=5):
    """Fetch the changelog index from docs.waldur.com with caching."""
    if not is_changelog_enabled():
        return None

    url = f"{CHANGELOG_BASE_URL}/index.json"
    return _fetch_json(url, timeout, CHANGELOG_INDEX_CACHE_KEY, CACHE_TIMEOUT_INDEX)


def fetch_changelog_release(version, timeout=5):
    """Fetch a specific release changelog from docs.waldur.com with caching."""
    if not is_changelog_enabled():
        return None

    cache_key = f"{CHANGELOG_RELEASE_CACHE_PREFIX}{version}"
    url = f"{CHANGELOG_BASE_URL}/releases/{version}.json"
    return _fetch_json(url, timeout, cache_key, CACHE_TIMEOUT_RELEASE)


def get_pending_versions(current_version, index_data=None):
    """Return versions newer than current_version from the index, sorted ascending."""
    if index_data is None:
        index_data = fetch_changelog_index()
    if not index_data or "releases" not in index_data:
        return []

    current = parse_version(current_version)
    if current is None:
        return []

    pending = []
    for release in index_data["releases"]:
        v = parse_version(release["version"])
        if v is not None and v > current:
            pending.append(release)

    pending.sort(key=lambda r: parse_version(r["version"]))
    return pending


def get_impact_analysis_target(pending):
    """Return the version compute_changelog_impact should be (or was) run
    against, given a get_pending_versions() list: the newest stable release
    if one is pending, else the newest release overall (which may be an
    RC) - never computing impact for every RC in a cycle, but never
    skipping analysis just because only RCs are pending either.

    Must be used everywhere a ChangelogImpactAnalysis is triggered or
    looked up by target_version - one side using pending[-1]["version"]
    while another used this function means an RC that's newer than the
    latest stable makes the two sides key the same analysis differently,
    so results never surface for the entire time an RC is the newest
    pending release.
    """
    if not pending:
        return None
    for release in reversed(pending):
        if release.get("type") == "stable":
            return release["version"]
    return pending[-1]["version"]


def select_new_entries(current_version, releases):
    """Pick, for each pending release, the entries this deployment hasn't seen.

    `releases` is a list of (release_info, release_data) pairs in ascending
    version order, as _fetch_releases returns them for get_pending_versions().
    A release's `entries` are cumulative since its base stable version, so
    concatenating them across an RC cycle repeats every change once per
    release. Instead, walk the chain: while each release's previous_version
    is the version just before it (the deployment's own version for the first
    one), its since_previous is exactly what is new. When the chain breaks - a
    release file is missing, or predates since_previous - fall back to the
    cumulative entries for that release. An RC carries its predecessors'
    entries with their original ids, so ids already selected are skipped.

    Returns (release_info, release_data, entries) triples in the same order.
    """
    selected = []
    selected_ids = set()
    seen = current_version
    for release_info, release_data in releases:
        delta = release_data.get("since_previous")
        if delta is not None and is_same_release(
            release_data.get("previous_version"), seen
        ):
            candidates = delta
        else:
            candidates = release_data.get("entries", [])
        entries = []
        for entry in candidates:
            entry_id = entry.get("id")
            if entry_id and entry_id in selected_ids:
                continue
            selected_ids.add(entry_id)
            entries.append(entry)
        selected.append((release_info, release_data, entries))
        seen = release_data.get("version", release_info["version"])
    return selected


def get_active_plugins():
    """Return list of active waldur extension plugin names from INSTALLED_APPS."""
    return [
        app
        for app in settings.INSTALLED_APPS
        if app.startswith("waldur_")
        and app != "waldur_core"
        and not app.startswith("waldur_core.")
    ]


def get_enabled_feature_flags():
    """Return dict of enabled feature flags, keyed 'section.feature'."""
    return {feature.key: True for feature in Feature.objects.filter(value=True)}


def get_customized_settings():
    """Return dict of constance settings that differ from their defaults."""
    try:
        constance_config = getattr(settings, "CONSTANCE_CONFIG", {})
        customized = {}
        for key, value_tuple in constance_config.items():
            default_value = value_tuple[0] if value_tuple else None
            current_value = getattr(config, key, None)
            if current_value != default_value:
                customized[key] = {
                    "current_value": current_value,
                    "default_value": default_value,
                }
        return customized
    except Exception:
        logger.warning("Failed to read constance settings for changelog matching")
        return {}


def _build_relevance_context():
    """Build the deployment context match_relevance() checks entries against.

    Each piece can involve real DB queries - feature flags are a query, and
    Constance settings are read per-key from a DB-backed store with no cache
    configured, so building this once per batch (see
    enrich_entries_with_relevance) instead of once per entry is the
    difference between a handful of queries and thousands.
    """
    return {
        "active_plugins": get_active_plugins(),
        "enabled_flags": get_enabled_feature_flags(),
        "customized_settings": get_customized_settings(),
    }


def match_relevance(entry, context=None):
    """Check if a changelog entry is relevant to this deployment.

    `context` is a _build_relevance_context() result; pass a shared one when
    checking many entries so its DB queries aren't repeated per entry (see
    enrich_entries_with_relevance). Built fresh if omitted.

    Returns (is_relevant, reasons) tuple.
    """
    relevant_when = entry.get("relevant_when", {})
    scope = entry.get("scope", "core")

    # Core and infra entries are always relevant
    if scope in ("core", "infra"):
        return True, []

    # Dev entries are not relevant in production views
    if scope == "dev":
        return False, ["Development/testing only"]

    plugins = relevant_when.get("plugins", [])
    feature_flags = relevant_when.get("feature_flags", [])
    entry_settings = relevant_when.get("settings", [])

    # If all arrays are empty, entry is always relevant
    if not plugins and not feature_flags and not entry_settings:
        return True, []

    if context is None:
        context = _build_relevance_context()

    reasons = []
    active_plugins = context["active_plugins"]
    enabled_flags = context["enabled_flags"]
    customized = context["customized_settings"]

    # Check plugins
    if plugins:
        matching = [p for p in plugins if p in active_plugins]
        if matching:
            reasons.extend(f"Plugin {p} is active" for p in matching)

    # Check feature flags
    if feature_flags:
        matching = [f for f in feature_flags if f in enabled_flags]
        if matching:
            reasons.extend(f"Feature flag {f} is enabled" for f in matching)

    # Check settings
    if entry_settings:
        matching = [s for s in entry_settings if s in customized]
        if matching:
            reasons.extend(f"Setting {s} is customized" for s in matching)

    return bool(reasons), reasons


def build_changelog_summary(index_data, current_version):
    """Build the compact changelog_summary for the /api/version/ response."""
    pending = get_pending_versions(current_version, index_data)
    if not pending:
        return None

    # The index only carries a per-release has_breaking flag, not per-entry
    # risk, so this counts releases with at least one breaking change - not
    # high-risk entries. Getting an actual entry-level count would mean
    # fetching every pending release's full payload on every /api/version/
    # call (index-only was a deliberate choice to keep that endpoint cheap).
    breaking_release_count = 0
    has_breaking = False
    security_entries = []

    for release in pending:
        if release.get("has_breaking"):
            has_breaking = True
            breaking_release_count += 1
        # Collect security alerts
        if release.get("has_security") and release.get("max_security_urgency") in (
            "critical",
            "high",
        ):
            security_entries.append(
                {
                    "version": release["version"],
                    "max_urgency": release.get("max_security_urgency"),
                }
            )

    summary = {
        "versions_behind": count_versions_behind(current_version, pending),
        "breaking_release_count": breaking_release_count,
        "has_breaking_changes": has_breaking,
    }

    if security_entries:
        max_urgency = (
            "critical"
            if any(s["max_urgency"] == "critical" for s in security_entries)
            else "high"
        )
        summary["security_alert"] = {
            "max_urgency": max_urgency,
            "count": len(security_entries),
            "versions": security_entries,
        }

    return summary


def enrich_entries_with_relevance(entries):
    """Add relevance info to a list of changelog entries."""
    context = _build_relevance_context()
    for entry in entries:
        is_relevant, reasons = match_relevance(entry, context)
        entry["relevant"] = is_relevant
        entry["relevance_reasons"] = reasons
    return entries


def merge_delta_entries(from_version, to_version, releases_data):
    """Merge since_previous entries from from_version to to_version.

    Used by the compare endpoint to show net changes between arbitrary versions.
    """
    from_v = parse_version(from_version)
    to_v = parse_version(to_version)
    if from_v is None or to_v is None:
        return []

    # Sort releases by version
    sorted_releases = sorted(
        releases_data,
        key=lambda r: parse_version(r["version"]) or Version("0"),
    )

    merged = []
    for release in sorted_releases:
        rv = parse_version(release["version"])
        if rv is None:
            continue
        if rv > from_v and rv <= to_v:
            merged.extend(release.get("since_previous", []))

    return merged
