import logging
from concurrent.futures import ThreadPoolExecutor

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import permissions as rf_permissions
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from waldur_core import __version__
from waldur_core.changelog import serializers
from waldur_core.changelog.models import ChangelogImpactAnalysis
from waldur_core.changelog.utils import (
    compare_versions,
    enrich_entries_with_relevance,
    fetch_changelog_release,
    get_impact_analysis_target,
    get_pending_versions,
    is_changelog_enabled,
    merge_delta_entries,
    parse_version,
)
from waldur_core.structure.permissions import IsStaffOrSupportUser

logger = logging.getLogger(__name__)

VALID_ENTRY_TYPES = {
    "breaking",
    "security",
    "deprecation",
    "feature",
    "improvement",
    "fix",
}
VALID_RISKS = {"high", "medium", "low", "none"}
VALID_SCOPES = {"core", "plugin", "infra", "dev"}


# Cap concurrent fetches so a deployment many versions behind doesn't open
# dozens of simultaneous connections to docs.waldur.com - bounded fan-out
# still turns a cold-cache request from "N releases x 5s timeout each,
# sequential" into roughly one timeout's worth of wall time.
MAX_CONCURRENT_RELEASE_FETCHES = 5


def _fetch_releases(release_infos):
    """Fetch changelog releases for the given index release_info dicts.

    Threads, not Celery: the request needs these results before it can
    respond, so there's nothing to hand off to a worker - it would just
    mean blocking on task results instead of thread results, with broker
    round-trips added on top.

    Fetches concurrently (each fetch_changelog_release call is an
    independent, cached outbound HTTP request), but returns results in the
    same order as release_infos regardless of completion order. Entries
    with no data (fetch failed) are omitted, matching the previous
    sequential "if not release_data: continue" behavior.
    """
    if not release_infos:
        return []
    with ThreadPoolExecutor(
        max_workers=min(len(release_infos), MAX_CONCURRENT_RELEASE_FETCHES)
    ) as executor:
        futures = [
            executor.submit(fetch_changelog_release, info["version"])
            for info in release_infos
        ]
        results = [future.result() for future in futures]
    return [(info, data) for info, data in zip(release_infos, results) if data]


def _not_enabled_response():
    return Response(
        {"detail": "Changelog is not enabled for this deployment."},
        status=404,
    )


def _invalid_version_response(version):
    return Response({"detail": f"Invalid version: {version}."}, status=404)


def _merge_impact_analysis(entries, current_version, target_version):
    """Merge impact analysis results into changelog entries if available."""
    try:
        analysis = ChangelogImpactAnalysis.objects.get(
            current_version=current_version,
            target_version=target_version,
        )
    except ChangelogImpactAnalysis.DoesNotExist:
        return entries, None

    for entry in entries:
        entry_id = entry.get("id")
        if entry_id and entry_id in analysis.results:
            entry.update(analysis.results[entry_id])

    return entries, analysis


@extend_schema(
    summary="Get pending changelog",
    description="Returns cumulative changelog entries for all versions newer than the current deployment, "
    "with relevance matching and impact analysis results.",
    responses={200: serializers.ChangelogPendingSerializer},
)
@api_view(["GET"])
@permission_classes([rf_permissions.IsAuthenticated, IsStaffOrSupportUser])
def changelog_pending(request):
    if not is_changelog_enabled():
        return _not_enabled_response()

    pending = get_pending_versions(__version__)
    if not pending:
        return Response(
            {
                "current_version": __version__,
                "latest_version": __version__,
                "versions_behind": 0,
                "releases": [],
            }
        )

    latest_version = pending[-1]["version"]
    stable_count = sum(1 for r in pending if r.get("type") == "stable")

    releases = []
    for _release_info, release_data in _fetch_releases(
        list(reversed(pending))
    ):  # newest first
        entries = enrich_entries_with_relevance(release_data.get("entries", []))
        # Sort: relevant first, then by risk
        risk_order = {"high": 0, "medium": 1, "low": 2, "none": 3}
        entries.sort(
            key=lambda e: (
                0 if e.get("relevant") else 1,
                risk_order.get(e.get("impact", {}).get("risk", "none"), 4),
            )
        )
        release_data["entries"] = entries
        releases.append(release_data)

    # Merge impact analysis if available. Looked up by the same target
    # compute_changelog_impact was actually triggered for (core/views.py's
    # _populate_changelog_fields), not latest_version - those differ
    # whenever an RC is the newest pending release, which would otherwise
    # mean the analysis is never found.
    all_entries = []
    for release in releases:
        all_entries.extend(release.get("entries", []))
    impact_target = get_impact_analysis_target(pending)
    all_entries, analysis = _merge_impact_analysis(
        all_entries, __version__, impact_target
    )

    response_data = {
        "current_version": __version__,
        "latest_version": latest_version,
        "versions_behind": stable_count,
        "releases": releases,
    }

    if analysis:
        response_data["impact_analysis_status"] = analysis.status
        response_data["impact_analysis_computed_at"] = analysis.computed_at

    serializer = serializers.ChangelogPendingSerializer(response_data)
    return Response(serializer.data)


@extend_schema(
    summary="Get changelog for a specific version",
    description="Returns the full changelog for a specific release version.",
    responses={200: serializers.ChangelogReleaseSerializer},
)
@api_view(["GET"])
@permission_classes([rf_permissions.IsAuthenticated, IsStaffOrSupportUser])
def changelog_detail(request, version):
    if not is_changelog_enabled():
        return _not_enabled_response()
    if parse_version(version) is None:
        return _invalid_version_response(version)

    release_data = fetch_changelog_release(version)
    if not release_data:
        return Response(
            {"detail": f"Changelog for version {version} not found."},
            status=404,
        )

    release_data["entries"] = enrich_entries_with_relevance(
        release_data.get("entries", [])
    )
    serializer = serializers.ChangelogReleaseSerializer(release_data)
    return Response(serializer.data)


@extend_schema(
    summary="Get changelog delta for a version",
    description="Returns only the incremental changes since the previous version (since_previous entries).",
    responses={200: serializers.ChangelogReleaseSerializer},
)
@api_view(["GET"])
@permission_classes([rf_permissions.IsAuthenticated, IsStaffOrSupportUser])
def changelog_delta(request, version):
    if not is_changelog_enabled():
        return _not_enabled_response()
    if parse_version(version) is None:
        return _invalid_version_response(version)

    release_data = fetch_changelog_release(version)
    if not release_data:
        return Response(
            {"detail": f"Changelog for version {version} not found."},
            status=404,
        )

    # Replace entries with since_previous for delta view
    delta_entries = release_data.get("since_previous", [])
    release_data["entries"] = enrich_entries_with_relevance(delta_entries)
    serializer = serializers.ChangelogReleaseSerializer(release_data)
    return Response(serializer.data)


@extend_schema(
    summary="Compare changelog between two versions",
    description="Returns merged delta entries between two arbitrary versions. "
    "Useful for seeing what changed between e.g. rc.5 and rc.12.",
    responses={200: serializers.ChangelogPendingSerializer},
)
@api_view(["GET"])
@permission_classes([rf_permissions.IsAuthenticated, IsStaffOrSupportUser])
def changelog_compare(request, from_version, to_version):
    if not is_changelog_enabled():
        return _not_enabled_response()

    # Fetch all releases in the range
    pending = get_pending_versions(from_version)
    if not pending:
        return Response(
            {
                "current_version": from_version,
                "latest_version": to_version,
                "versions_behind": 0,
                "releases": [],
            }
        )

    # Fetch release data for versions in range
    in_range = [
        release_info
        for release_info in pending
        if compare_versions(release_info["version"], to_version) <= 0
    ]
    releases_data = [data for _info, data in _fetch_releases(in_range)]

    merged_entries = merge_delta_entries(from_version, to_version, releases_data)
    merged_entries = enrich_entries_with_relevance(merged_entries)

    response_data = {
        "current_version": from_version,
        "latest_version": to_version,
        "versions_behind": len(releases_data),
        "releases": [
            {
                "version": f"{from_version}..{to_version}",
                "date": "",
                "type": "comparison",
                "summary": f"Changes between {from_version} and {to_version}",
                "entries": merged_entries,
            }
        ],
    }

    serializer = serializers.ChangelogPendingSerializer(response_data)
    return Response(serializer.data)


@extend_schema(
    summary="List changelog entries",
    description="Returns a flat, paginated list of changelog entries for all pending versions. "
    "Supports filtering by type, risk, scope, version, and text search. "
    "Compatible with the standard Waldur table component.",
    parameters=[
        OpenApiParameter("type", str, description="Filter by entry type"),
        OpenApiParameter("risk", str, description="Filter by risk level"),
        OpenApiParameter("scope", str, description="Filter by scope"),
        OpenApiParameter("version", str, description="Filter by release version"),
        OpenApiParameter(
            "highlight", bool, description="Filter highlighted entries only"
        ),
        OpenApiParameter(
            "relevant_only", bool, description="Show only relevant entries"
        ),
        OpenApiParameter(
            "search", str, description="Full-text search in title and description"
        ),
        OpenApiParameter("page", int, description="Page number"),
        OpenApiParameter("page_size", int, description="Items per page"),
    ],
    responses={200: serializers.ChangelogEntryListSerializer},
)
@api_view(["GET"])
@permission_classes([rf_permissions.IsAuthenticated, IsStaffOrSupportUser])
def changelog_entries_list(request):
    """Flat paginated list of changelog entries for table display."""
    if not is_changelog_enabled():
        return _not_enabled_response()

    # Collect all entries from pending versions
    pending = get_pending_versions(__version__)
    all_entries = []
    for release_info, release_data in _fetch_releases(pending):
        for entry in release_data.get("entries", []):
            entry["version"] = release_info["version"]
            entry["release_date"] = release_info.get("date", "")
            entry["release_type"] = release_info.get("type", "stable")
            all_entries.append(entry)

    # Enrich with relevance
    all_entries = enrich_entries_with_relevance(all_entries)

    # Merge impact analysis - looked up by the same target the analysis was
    # actually triggered for, not pending[-1]["version"] (see changelog_pending).
    if pending:
        impact_target = get_impact_analysis_target(pending)
        all_entries, _ = _merge_impact_analysis(all_entries, __version__, impact_target)

    # Apply filters
    params = request.query_params
    entry_type = params.get("type")
    if entry_type and entry_type in VALID_ENTRY_TYPES:
        all_entries = [e for e in all_entries if e.get("type") == entry_type]

    risk = params.get("risk")
    if risk and risk in VALID_RISKS:
        all_entries = [
            e for e in all_entries if e.get("impact", {}).get("risk") == risk
        ]

    scope = params.get("scope")
    if scope and scope in VALID_SCOPES:
        all_entries = [e for e in all_entries if e.get("scope") == scope]

    version = params.get("version")
    if version:
        all_entries = [e for e in all_entries if e.get("version") == version]

    if params.get("highlight") in ("true", "1"):
        all_entries = [e for e in all_entries if e.get("highlight")]

    if params.get("relevant_only") in ("true", "1"):
        all_entries = [e for e in all_entries if e.get("relevant") is not False]

    search = params.get("search", "").lower()
    if search:
        all_entries = [
            e
            for e in all_entries
            if search in e.get("title", "").lower()
            or search in e.get("description", "").lower()
        ]

    # Sort: relevant first, then by risk
    risk_order = {"high": 0, "medium": 1, "low": 2, "none": 3}
    all_entries.sort(
        key=lambda e: (
            0 if e.get("relevant") else 1,
            risk_order.get(e.get("impact", {}).get("risk", "none"), 4),
        )
    )

    # Paginate
    try:
        page = int(params.get("page", 1))
    except ValueError:
        page = 1
    page = max(page, 1)

    try:
        page_size = int(params.get("page_size", 50))
    except ValueError:
        page_size = 50
    page_size = min(max(page_size, 1), 200)

    start = (page - 1) * page_size
    end = start + page_size
    paginated = all_entries[start:end]

    response_data = {
        "count": len(all_entries),
        "current_version": __version__,
        "latest_version": pending[-1]["version"] if pending else __version__,
        "versions_behind": sum(1 for r in pending if r.get("type") == "stable"),
        "results": paginated,
    }
    serializer = serializers.ChangelogEntryListSerializer(response_data)
    return Response(serializer.data)
