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

from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import (
    COIDetectionMethods,
    COISeverityLevels,
    COIStatuses,
    COITypes,
)

logger = logging.getLogger(__name__)


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
    """
    Extract team members from a proposal.

    Returns list of dictionaries with user info, ORCID, email, and name.
    """
    team_members = []

    # Get from proposal personnel/applicants
    if hasattr(proposal, "projectindication"):
        indication = proposal.projectindication
        if indication.project_pi:
            team_members.append(
                {
                    "user": indication.project_pi,
                    "name": indication.project_pi.full_name,
                    "email": indication.project_pi.email,
                    "orcid": getattr(indication.project_pi, "orcid_id", None),
                    "role": "PI",
                }
            )

    # Get from proposal team members if they exist
    if hasattr(proposal, "team_members"):
        for member in proposal.team_members.all():
            if hasattr(member, "user") and member.user:
                team_members.append(
                    {
                        "user": member.user,
                        "name": member.user.full_name,
                        "email": member.user.email,
                        "orcid": getattr(member.user, "orcid_id", None),
                        "role": getattr(member, "role", "Team Member"),
                    }
                )

    # Include project customer as an organization
    if proposal.project and proposal.project.customer:
        team_members.append(
            {
                "organization": proposal.project.customer,
                "name": proposal.project.customer.name,
                "role": "Applicant Organization",
            }
        )

    return team_members


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


def detect_coauthorship_conflicts(
    reviewer: "models.ReviewerProfile",
    proposal: "models.Proposal",
    config: "models.CallCOIConfiguration | None" = None,
) -> list["models.ConflictOfInterest"]:
    """
    Detect COI based on co-authorship between reviewer and proposal team.

    Algorithm:
    1. Get all proposal team members (PI, co-PIs, key personnel)
    2. For each team member, check for shared publications with reviewer
    3. Apply lookback window and threshold from config
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
    reviewer_pubs = models.ReviewerPublication.objects.filter(
        reviewer_profile=reviewer,
        publication_year__gte=cutoff_year,
    )

    if not reviewer_pubs.exists():
        return conflicts

    # Build sets for comparison
    {p.doi.lower() for p in reviewer_pubs if p.doi}
    reviewer_coauthors = []
    for pub in reviewer_pubs:
        if pub.coauthors:
            for coauthor in pub.coauthors:
                if isinstance(coauthor, dict):
                    name = coauthor.get("name", "")
                    orcid = coauthor.get("orcid")
                    reviewer_coauthors.append(
                        {"name": name, "orcid": orcid, "pub": pub}
                    )
                elif isinstance(coauthor, str):
                    reviewer_coauthors.append(
                        {"name": coauthor, "orcid": None, "pub": pub}
                    )

    # Check against proposal team members
    team_members = get_proposal_team_members(proposal)

    for member in team_members:
        if "organization" in member:
            continue  # Skip organizations for coauthorship check

        member_name = member.get("name", "")
        member_orcid = member.get("orcid")
        member.get("email")
        member_user = member.get("user")

        shared_publications = []

        # Check if member appears in reviewer's coauthors
        for coauthor_info in reviewer_coauthors:
            coauthor_name = coauthor_info.get("name", "")
            coauthor_orcid = coauthor_info.get("orcid")
            pub = coauthor_info.get("pub")

            # ORCID match (most reliable)
            if member_orcid and coauthor_orcid and member_orcid == coauthor_orcid:
                shared_publications.append(
                    {
                        "title": pub.title,
                        "doi": pub.doi,
                        "year": pub.publication_year,
                        "match_type": "orcid",
                    }
                )
                continue

            # Name match (fuzzy)
            if fuzzy_name_match(member_name, coauthor_name):
                shared_publications.append(
                    {
                        "title": pub.title,
                        "doi": pub.doi,
                        "year": pub.publication_year,
                        "match_type": "name",
                    }
                )

        # Deduplicate by DOI
        seen_dois = set()
        unique_publications = []
        for pub in shared_publications:
            key = pub.get("doi") or pub.get("title")
            if key and key not in seen_dois:
                seen_dois.add(key)
                unique_publications.append(pub)

        if len(unique_publications) >= threshold_papers:
            # Determine severity based on recency and count
            is_recent = any(
                p.get("year", 0) >= date.today().year - 1 for p in unique_publications
            )
            severity = (
                COISeverityLevels.REAL
                if len(unique_publications) >= 3 and is_recent
                else COISeverityLevels.APPARENT
            )
            coi_type = COITypes.COAUTH_RECENT if is_recent else COITypes.COAUTH_OLD

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
                        "team_member_role": member.get("role"),
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
                severity = COISeverityLevels.REAL
                description = (
                    f"Reviewer currently affiliated with {affil.organization_name}"
                )
            else:
                coi_type = COITypes.INST_FORMER
                severity = COISeverityLevels.APPARENT
                description = (
                    f"Reviewer was previously at {affil.organization_name} "
                    f"until {affil.end_date}"
                )

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


def detect_named_personnel_conflicts(
    reviewer: "models.ReviewerProfile",
    proposal: "models.Proposal",
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
        if "organization" in member:
            continue

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
                    severity=COISeverityLevels.REAL,
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
        conflicts = detect_named_personnel_conflicts(reviewer, proposal)
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
