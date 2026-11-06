"""
COI Detection Service for Waldur Proposal Module.

This module provides automated conflict of interest detection algorithms:
- Co-authorship detection based on shared publications
- Institutional affiliation detection
- Named personnel detection (reviewer named in proposal)
"""

import logging
from datetime import date, timedelta
from difflib import SequenceMatcher

from django.db.models import Q
from django.utils import timezone

from waldur_core.permissions.models import UserRole
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import (
    COIDetectionMethods,
    COISeverityLevels,
    COIStatuses,
    COITypes,
)

logger = logging.getLogger(__name__)

# How each of the call's type-handling rules is recorded, by the severity
# level whose meaning it is (see ``COISeverityLevels``).
HANDLING_SEVERITIES = {
    "recusal_required_types": COISeverityLevels.REAL,
    "management_allowed_types": COISeverityLevels.APPARENT,
    "disclosure_only_types": COISeverityLevels.POTENTIAL,
}


def normalize_name(name: str) -> str:
    """Normalize a name for comparison."""
    if not name:
        return ""
    return " ".join(name.lower().split())


def fuzzy_name_match(name1: str, name2: str, threshold: float = 0.85) -> bool:
    """
    Check if two names are similar enough to be considered a match.

    Uses SequenceMatcher for fuzzy string matching.
    """
    if not name1 or not name2:
        return False

    norm1 = normalize_name(name1)
    norm2 = normalize_name(name2)

    # Exact match after normalization
    if norm1 == norm2:
        return True

    # Fuzzy match
    ratio = SequenceMatcher(None, norm1, norm2).ratio()
    return ratio >= threshold


def get_proposal_team_members(proposal: "models.Proposal") -> list:
    """The proposal's applicant team, as people to match a reviewer against.

    The team is everyone holding an active role on the proposal (see
    ``proposal_roles.applicant_team_user_ids``); its author is on it only
    while they still hold one. A member's ORCID, name variants, publications
    and affiliations come from their reviewer profile when they have one --
    the user account carries none of them.

    Returns one dictionary per member with the user, name, email, ORCID,
    alternative names, reviewer profile (or ``None``) and role.
    """
    team_members = {}
    roles = (
        UserRole.objects.filter(is_active=True, scope=proposal)
        .select_related("user", "user__reviewer_profile", "role")
        .order_by("user_id", "id")
    )
    for user_role in roles:
        user = user_role.user
        if user.id in team_members:
            continue
        profile = getattr(user, "reviewer_profile", None)
        team_members[user.id] = {
            "user": user,
            "name": user.full_name,
            "email": user.email,
            "orcid": profile.orcid_id if profile else None,
            "alternative_names": (profile.alternative_names or []) if profile else [],
            "profile": profile,
            "role": user_role.role.name,
        }
    return list(team_members.values())


def default_severity(
    coi_type: str, config: "models.CallCOIConfiguration | None" = None
) -> str:
    """The severity a conflict of ``coi_type`` is recorded with.

    The call's type handling decides when it names the type -- types
    requiring recusal are real conflicts, those allowing a management plan
    apparent, those needing disclosure only potential, matching the three
    severity levels' own meanings -- and the type's default otherwise.
    """
    if config is not None:
        for field, severity in HANDLING_SEVERITIES.items():
            if coi_type in (getattr(config, field) or []):
                return severity
    return COITypes.SEVERITY_MAP.get(coi_type, COISeverityLevels.APPARENT)


def is_handled_by_call(
    coi_type: str, config: "models.CallCOIConfiguration | None"
) -> bool:
    return config is not None and any(
        coi_type in (getattr(config, field) or []) for field in HANDLING_SEVERITIES
    )


def requires_recusal(conflict: "models.ConflictOfInterest") -> bool:
    """Whether the call's type handling makes the conflict's type one the
    reviewer must withdraw for, so that no management plan can waive it."""
    config = models.CallCOIConfiguration.objects.filter(
        call_id=conflict.call_id
    ).first()
    return bool(config) and conflict.coi_type in (config.recusal_required_types or [])


def get_proposal_organizations(proposal: "models.Proposal") -> list:
    """Get organizations associated with a proposal."""
    organizations = []

    if proposal.project and proposal.project.customer:
        organizations.append(proposal.project.customer)

    return organizations


def normalize_org_identifier(org) -> str | None:
    """
    Normalize organization identifier for comparison.

    Handles both Customer objects and ReviewerAffiliation objects.
    """
    if hasattr(org, "uuid"):
        return str(org.uuid)
    if hasattr(org, "organization") and org.organization:
        return str(org.organization.uuid)
    if hasattr(org, "organization_identifier") and org.organization_identifier:
        return org.organization_identifier.lower().strip()
    if hasattr(org, "organization_name"):
        return normalize_name(org.organization_name)
    if hasattr(org, "name"):
        return normalize_name(org.name)
    return None


def _normalize_doi(doi) -> str:
    return (doi or "").strip().lower()


def detect_coauthorship_conflicts(
    reviewer: "models.ReviewerProfile",
    proposal: "models.Proposal",
    config: "models.CallCOIConfiguration | None" = None,
) -> list["models.ConflictOfInterest"]:
    """
    Detect COI based on co-authorship between reviewer and proposal team.

    A reviewer's publication within the lookback window is shared with a
    team member when its coauthor list names them (by ORCID, name or one of
    their name variants), or when it is on the member's own publication list
    too (same DOI). Enough shared papers (the call's threshold) is a conflict.
    """
    conflicts = []
    call = proposal.round.call if hasattr(proposal, "round") else None

    if not call:
        logger.warning(f"Proposal {proposal.uuid} has no associated call")
        return conflicts

    # Get configuration or use defaults
    lookback_years = config.coauthorship_lookback_years if config else 3
    threshold_papers = config.coauthorship_threshold_papers if config else 1

    cutoff_year = date.today().year - lookback_years

    # Get reviewer's publications within lookback period
    reviewer_pubs = list(
        models.ReviewerPublication.objects.filter(
            reviewer_profile=reviewer,
            publication_year__gte=cutoff_year,
        )
    )

    if not reviewer_pubs:
        return conflicts

    reviewer_pubs_by_doi = {}
    reviewer_coauthors = []
    for pub in reviewer_pubs:
        if pub.doi:
            reviewer_pubs_by_doi.setdefault(_normalize_doi(pub.doi), pub)
        for coauthor in pub.coauthors or []:
            if isinstance(coauthor, dict):
                reviewer_coauthors.append(
                    {
                        "name": coauthor.get("name", ""),
                        "orcid": coauthor.get("orcid"),
                        "pub": pub,
                    }
                )
            elif isinstance(coauthor, str):
                reviewer_coauthors.append({"name": coauthor, "orcid": None, "pub": pub})

    def shared(pub, match_type):
        return {
            "title": pub.title,
            "doi": pub.doi,
            "year": pub.publication_year,
            "match_type": match_type,
        }

    for member in get_proposal_team_members(proposal):
        member_user = member["user"]
        # The reviewer on the team is named personnel, not their own coauthor.
        if member_user.id == reviewer.user_id:
            continue

        member_name = member["name"]
        member_names = [member_name, *member["alternative_names"]]
        member_orcid = member["orcid"]

        shared_publications = []

        # Check if member appears in reviewer's coauthors
        for coauthor_info in reviewer_coauthors:
            coauthor_orcid = coauthor_info["orcid"]
            pub = coauthor_info["pub"]

            # ORCID match (most reliable)
            if member_orcid and coauthor_orcid and member_orcid == coauthor_orcid:
                shared_publications.append(shared(pub, "orcid"))
            elif any(
                fuzzy_name_match(name, coauthor_info["name"]) for name in member_names
            ):
                shared_publications.append(shared(pub, "name"))

        # The same paper on both publication lists
        if member["profile"] and reviewer_pubs_by_doi:
            for doi in models.ReviewerPublication.objects.filter(
                reviewer_profile=member["profile"]
            ).values_list("doi", flat=True):
                pub = reviewer_pubs_by_doi.get(_normalize_doi(doi))
                if pub:
                    shared_publications.append(shared(pub, "doi"))

        # Deduplicate by DOI
        seen_dois = set()
        unique_publications = []
        for pub in shared_publications:
            key = _normalize_doi(pub.get("doi")) or pub.get("title")
            if key and key not in seen_dois:
                seen_dois.add(key)
                unique_publications.append(pub)

        if len(unique_publications) >= threshold_papers:
            is_recent = any(
                p.get("year", 0) >= date.today().year - 1 for p in unique_publications
            )
            coi_type = COITypes.COAUTH_RECENT if is_recent else COITypes.COAUTH_OLD
            severity = default_severity(coi_type, config)
            # Three or more recent papers together is a real conflict, unless
            # the call says how the type is handled.
            if (
                coi_type == COITypes.COAUTH_RECENT
                and len(unique_publications) >= 3
                and not is_handled_by_call(coi_type, config)
            ):
                severity = COISeverityLevels.REAL

            # Check if this COI already exists
            existing = models.ConflictOfInterest.objects.filter(
                reviewer=reviewer,
                proposal=proposal,
                coi_type=coi_type,
                conflicting_user=member_user,
            ).first()

            if not existing:
                conflict = models.ConflictOfInterest(
                    reviewer=reviewer,
                    proposal=proposal,
                    call=call,
                    conflicting_user=member_user,
                    coi_type=coi_type,
                    severity=severity,
                    detection_method=COIDetectionMethods.AUTOMATED,
                    evidence_description=(
                        f"Found {len(unique_publications)} shared publication(s) "
                        f"in past {lookback_years} years with {member_name}"
                    ),
                    evidence_data={
                        "shared_publications": unique_publications,
                        "lookback_years": lookback_years,
                        "team_member_name": member_name,
                        "team_member_role": member["role"],
                    },
                    status=COIStatuses.PENDING,
                )
                conflicts.append(conflict)

    return conflicts


def detect_institutional_conflicts(
    reviewer: "models.ReviewerProfile",
    proposal: "models.Proposal",
    config: "models.CallCOIConfiguration | None" = None,
) -> list["models.ConflictOfInterest"]:
    """
    Detect COI based on institutional affiliations.

    Checks:
    1. Current same-institution employment
    2. Same department (if configured)
    3. Recent former affiliation (within lookback window)
    """
    conflicts = []
    call = proposal.round.call if hasattr(proposal, "round") else None

    if not call:
        return conflicts

    # Get configuration or use defaults
    lookback_years = config.institutional_lookback_years if config else 2
    include_same_institution = config.include_same_institution if config else True
    include_same_department = config.include_same_department if config else True

    if include_same_department:
        conflicts.extend(_detect_same_department_conflicts(reviewer, proposal, config))

    if not include_same_institution:
        return conflicts

    cutoff_date = date.today() - timedelta(days=lookback_years * 365)

    # Get applicant organizations
    applicant_orgs = get_proposal_organizations(proposal)
    if not applicant_orgs:
        return conflicts

    applicant_org_ids = set()
    applicant_org_names = set()
    for org in applicant_orgs:
        if hasattr(org, "uuid"):
            applicant_org_ids.add(str(org.uuid))
        if hasattr(org, "name"):
            applicant_org_names.add(normalize_name(org.name))

    # Get reviewer's affiliations (current and recent)
    reviewer_affiliations = models.ReviewerAffiliation.objects.filter(
        reviewer_profile=reviewer
    ).filter(Q(end_date__isnull=True) | Q(end_date__gte=cutoff_date))

    for affil in reviewer_affiliations:
        matched_org = None

        # Check if affiliation matches any applicant organization
        if affil.organization and str(affil.organization.uuid) in applicant_org_ids:
            matched_org = affil.organization
        elif normalize_name(affil.organization_name) in applicant_org_names:
            matched_org = affil.organization or affil.organization_name

        if matched_org:
            is_current = affil.end_date is None

            if is_current:
                coi_type = COITypes.INST_SAME
                description = (
                    f"Reviewer currently affiliated with {affil.organization_name}"
                )
            else:
                coi_type = COITypes.INST_FORMER
                description = (
                    f"Reviewer was previously at {affil.organization_name} "
                    f"until {affil.end_date}"
                )

            severity = default_severity(coi_type, config)

            # Check if this COI already exists
            existing = models.ConflictOfInterest.objects.filter(
                reviewer=reviewer,
                proposal=proposal,
                coi_type=coi_type,
            ).first()

            if not existing:
                conflict = models.ConflictOfInterest(
                    reviewer=reviewer,
                    proposal=proposal,
                    call=call,
                    conflicting_organization=(
                        affil.organization
                        if affil.organization
                        else (applicant_orgs[0] if applicant_orgs else None)
                    ),
                    coi_type=coi_type,
                    severity=severity,
                    detection_method=COIDetectionMethods.AUTOMATED,
                    evidence_description=description,
                    evidence_data={
                        "affiliation_type": affil.affiliation_type,
                        "position": affil.position_title,
                        "department": affil.department,
                        "start_date": str(affil.start_date),
                        "end_date": str(affil.end_date) if affil.end_date else None,
                        "is_current": is_current,
                    },
                    status=COIStatuses.PENDING,
                )
                conflicts.append(conflict)

    return conflicts


def _current_affiliations(profile):
    today = date.today()
    return models.ReviewerAffiliation.objects.filter(
        Q(end_date__isnull=True) | Q(end_date__gte=today),
        reviewer_profile=profile,
    ).select_related("organization")


def _same_organization(first, second) -> bool:
    if first.organization_id and second.organization_id:
        return first.organization_id == second.organization_id
    first_name = normalize_name(first.organization_name)
    return bool(first_name) and first_name == normalize_name(second.organization_name)


def _detect_same_department_conflicts(reviewer, proposal, config=None):
    """Same department: the reviewer and a team member are both currently
    affiliated with the same organisation and the same department.

    Only the reviewer profile records departments, so a team member is
    compared only when they have a reviewer profile listing affiliations.
    """
    conflicts = []
    call = proposal.round.call
    reviewer_affiliations = [
        affil for affil in _current_affiliations(reviewer) if affil.department.strip()
    ]
    if not reviewer_affiliations:
        return conflicts

    for member in get_proposal_team_members(proposal):
        member_user = member["user"]
        if not member["profile"] or member_user.id == reviewer.user_id:
            continue
        match = next(
            (
                (affil, member_affil)
                for member_affil in _current_affiliations(member["profile"])
                for affil in reviewer_affiliations
                if normalize_name(member_affil.department)
                and normalize_name(member_affil.department)
                == normalize_name(affil.department)
                and _same_organization(affil, member_affil)
            ),
            None,
        )
        if not match:
            continue
        affil, member_affil = match
        if models.ConflictOfInterest.objects.filter(
            reviewer=reviewer,
            proposal=proposal,
            coi_type=COITypes.INST_DEPT,
            conflicting_user=member_user,
        ).exists():
            continue
        conflicts.append(
            models.ConflictOfInterest(
                reviewer=reviewer,
                proposal=proposal,
                call=call,
                conflicting_user=member_user,
                conflicting_organization=affil.organization
                or member_affil.organization,
                coi_type=COITypes.INST_DEPT,
                severity=default_severity(COITypes.INST_DEPT, config),
                detection_method=COIDetectionMethods.AUTOMATED,
                evidence_description=(
                    f"Reviewer and {member['name']} are both in {affil.department} "
                    f"at {affil.organization_name}"
                ),
                evidence_data={
                    "department": affil.department,
                    "organization": affil.organization_name,
                    "team_member_name": member["name"],
                    "team_member_role": member["role"],
                },
                status=COIStatuses.PENDING,
            )
        )
    return conflicts


def detect_named_personnel_conflicts(
    reviewer: "models.ReviewerProfile",
    proposal: "models.Proposal",
    config: "models.CallCOIConfiguration | None" = None,
) -> list["models.ConflictOfInterest"]:
    """
    Detect if reviewer is named in proposal personnel.

    Checks proposal for:
    - User ID match
    - ORCID match
    - Name match (fuzzy)
    - Email match
    """
    conflicts = []
    call = proposal.round.call if hasattr(proposal, "round") else None

    if not call:
        return conflicts

    reviewer_user = reviewer.user
    reviewer_name = reviewer_user.full_name
    reviewer_email = reviewer_user.email.lower() if reviewer_user.email else None
    reviewer_orcid = reviewer.orcid_id

    # Include alternative names
    alternative_names = reviewer.alternative_names or []

    # Get proposal team members
    team_members = get_proposal_team_members(proposal)

    for member in team_members:
        match_reason = None
        member_user = member.get("user")
        member_name = member.get("name", "")
        member_email = member.get("email", "").lower() if member.get("email") else None
        member_orcid = member.get("orcid")

        # User ID match (most reliable)
        if member_user and member_user.id == reviewer_user.id:
            match_reason = "User account match"
        # ORCID match
        elif reviewer_orcid and member_orcid and reviewer_orcid == member_orcid:
            match_reason = f"ORCID match: {reviewer_orcid}"
        # Email match
        elif reviewer_email and member_email and reviewer_email == member_email:
            match_reason = f"Email match: {reviewer_email}"
        # Name match
        elif fuzzy_name_match(reviewer_name, member_name, threshold=0.9):
            match_reason = f"Name match: {member_name}"
        else:
            # Check alternative names
            for alt_name in alternative_names:
                if fuzzy_name_match(alt_name, member_name, threshold=0.9):
                    match_reason = f"Alternative name match: {alt_name}"
                    break

        if match_reason:
            # Check if this COI already exists
            existing = models.ConflictOfInterest.objects.filter(
                reviewer=reviewer,
                proposal=proposal,
                coi_type=COITypes.ROLE_NAMED,
            ).first()

            if not existing:
                conflict = models.ConflictOfInterest(
                    reviewer=reviewer,
                    proposal=proposal,
                    call=call,
                    conflicting_user=member_user,
                    coi_type=COITypes.ROLE_NAMED,
                    severity=default_severity(COITypes.ROLE_NAMED, config),
                    detection_method=COIDetectionMethods.AUTOMATED,
                    evidence_description=f"Reviewer appears to be named on proposal: {match_reason}",
                    evidence_data={
                        "match_reason": match_reason,
                        "role_in_proposal": member.get("role"),
                        "matched_name": member_name,
                    },
                    status=COIStatuses.PENDING,
                )
                conflicts.append(conflict)
                break  # One match is enough

    return conflicts


def run_coi_detection_for_pair(
    reviewer: "models.ReviewerProfile",
    proposal: "models.Proposal",
    config: "models.CallCOIConfiguration | None" = None,
) -> list["models.ConflictOfInterest"]:
    """
    Run all COI detection algorithms for a reviewer-proposal pair.

    Returns list of detected conflicts (not yet saved).
    """
    all_conflicts = []

    # Named personnel check (always run)
    if config is None or config.auto_detect_named_personnel:
        conflicts = detect_named_personnel_conflicts(reviewer, proposal, config)
        all_conflicts.extend(conflicts)

    # Institutional affiliation check
    if config is None or config.auto_detect_institutional:
        conflicts = detect_institutional_conflicts(reviewer, proposal, config)
        all_conflicts.extend(conflicts)

    # Co-authorship check
    if config is None or config.auto_detect_coauthorship:
        conflicts = detect_coauthorship_conflicts(reviewer, proposal, config)
        all_conflicts.extend(conflicts)

    return all_conflicts


def run_coi_detection_for_call(
    call: "models.Call",
    job: "models.COIDetectionJob | None" = None,
) -> dict:
    """
    Run COI detection for all reviewer-proposal pairs in a call.

    Returns summary of detection results.
    """
    from waldur_mastermind.proposal.enums import (
        COIDetectionJobStates,
        ReviewerPoolInvitationStatuses,
    )

    # Get COI configuration for this call
    config = getattr(call, "coi_configuration", None)

    # Get active reviewers in the pool
    pool_members = models.CallReviewerPool.objects.filter(
        call=call,
        invitation_status=ReviewerPoolInvitationStatuses.ACCEPTED,
    ).select_related("reviewer")

    # Get proposals for this call
    proposals = models.Proposal.objects.filter(round__call=call)

    total_pairs = pool_members.count() * proposals.count()
    processed = 0
    conflicts_found = 0
    created_conflicts = []

    if job:
        job.total_pairs = total_pairs
        job.state = COIDetectionJobStates.RUNNING
        job.started_at = timezone.now()
        job.config_snapshot = {
            # Detection thresholds
            "coauthorship_lookback_years": config.coauthorship_lookback_years
            if config
            else 3,
            "coauthorship_threshold_papers": config.coauthorship_threshold_papers
            if config
            else 1,
            "institutional_lookback_years": config.institutional_lookback_years
            if config
            else 2,
            # Detection toggles
            "auto_detect_coauthorship": config.auto_detect_coauthorship
            if config
            else True,
            "auto_detect_institutional": config.auto_detect_institutional
            if config
            else True,
            "auto_detect_named_personnel": config.auto_detect_named_personnel
            if config
            else True,
            "include_same_department": config.include_same_department
            if config
            else True,
            "include_same_institution": config.include_same_institution
            if config
            else True,
            # COI type classifications (for audit trail)
            "recusal_required_types": config.recusal_required_types if config else [],
            "management_allowed_types": config.management_allowed_types
            if config
            else [],
            "disclosure_only_types": config.disclosure_only_types if config else [],
        }
        job.save()

    try:
        for pool_member in pool_members:
            reviewer = pool_member.reviewer

            for proposal in proposals:
                conflicts = run_coi_detection_for_pair(reviewer, proposal, config)

                for conflict in conflicts:
                    conflict.save()
                    created_conflicts.append(conflict)
                    conflicts_found += 1

                processed += 1

                if job and processed % 10 == 0:
                    job.processed_pairs = processed
                    job.conflicts_found = conflicts_found
                    job.save(update_fields=["processed_pairs", "conflicts_found"])

        if job:
            job.state = COIDetectionJobStates.COMPLETED
            job.processed_pairs = processed
            job.conflicts_found = conflicts_found
            job.completed_at = timezone.now()
            job.save()

    except Exception as e:
        logger.exception(f"COI detection failed for call {call.uuid}")
        if job:
            job.state = COIDetectionJobStates.FAILED
            job.error_message = str(e)
            job.save()
        raise

    return {
        "total_pairs": total_pairs,
        "processed": processed,
        "conflicts_found": conflicts_found,
        "created_conflicts": [str(c.uuid) for c in created_conflicts],
    }
