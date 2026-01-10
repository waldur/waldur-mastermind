"""
ORCID Integration Service for Waldur Reviewer Profiles.

This module provides OAuth2 authentication and data synchronization with ORCID.
ORCID (Open Researcher and Contributor ID) is a unique identifier for researchers
that helps connect research contributions to their authors.

Configuration is managed through Constance settings:
- ORCID_CLIENT_ID: OAuth2 client ID
- ORCID_CLIENT_SECRET: OAuth2 client secret
- ORCID_REDIRECT_URI: OAuth2 redirect URI
- ORCID_API_URL: ORCID API base URL
- ORCID_AUTH_URL: ORCID OAuth authorization URL
- ORCID_SANDBOX_MODE: Use sandbox environment
"""

import logging
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

import requests
from constance import config
from django.utils import timezone

from waldur_mastermind.proposal.enums import (
    PublicationVenueTypes,
    ReviewerAffiliationTypes,
)

logger = logging.getLogger(__name__)

# ORCID API timeout in seconds
ORCID_REQUEST_TIMEOUT = 30


class ORCIDError(Exception):
    """Base exception for ORCID-related errors."""

    pass


class ORCIDAuthError(ORCIDError):
    """Exception for ORCID authentication errors."""

    pass


class ORCIDAPIError(ORCIDError):
    """Exception for ORCID API errors."""

    pass


def get_orcid_urls() -> dict[str, str]:
    """
    Get ORCID URLs based on configuration.

    Returns:
        Dictionary with auth_url and api_url.
    """
    if config.ORCID_SANDBOX_MODE:
        return {
            "auth_url": "https://sandbox.orcid.org/oauth",
            "api_url": "https://pub.sandbox.orcid.org/v3.0",
        }
    return {
        "auth_url": config.ORCID_AUTH_URL or "https://orcid.org/oauth",
        "api_url": config.ORCID_API_URL or "https://pub.orcid.org/v3.0",
    }


def is_orcid_configured() -> bool:
    """Check if ORCID integration is properly configured."""
    return bool(
        config.ORCID_CLIENT_ID
        and config.ORCID_CLIENT_SECRET
        and config.ORCID_REDIRECT_URI
    )


def get_authorization_url(state: str | None = None) -> str:
    """
    Generate ORCID OAuth2 authorization URL.

    Args:
        state: Optional state parameter for CSRF protection.

    Returns:
        Authorization URL to redirect user to.

    Raises:
        ORCIDError: If ORCID is not configured.
    """
    if not is_orcid_configured():
        raise ORCIDError("ORCID integration is not configured.")

    urls = get_orcid_urls()
    params = {
        "client_id": config.ORCID_CLIENT_ID,
        "response_type": "code",
        "scope": "/read-limited",
        "redirect_uri": config.ORCID_REDIRECT_URI,
    }
    if state:
        params["state"] = state

    return f"{urls['auth_url']}/authorize?{urlencode(params)}"


def exchange_code_for_token(code: str) -> dict[str, Any]:
    """
    Exchange authorization code for access token.

    Args:
        code: Authorization code from OAuth callback.

    Returns:
        Dictionary containing access_token, refresh_token, orcid, and expires_in.

    Raises:
        ORCIDAuthError: If token exchange fails.
    """
    if not is_orcid_configured():
        raise ORCIDError("ORCID integration is not configured.")

    urls = get_orcid_urls()
    token_url = f"{urls['auth_url']}/token"

    data = {
        "client_id": config.ORCID_CLIENT_ID,
        "client_secret": config.ORCID_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.ORCID_REDIRECT_URI,
    }

    try:
        response = requests.post(
            token_url,
            data=data,
            headers={"Accept": "application/json"},
            timeout=ORCID_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"ORCID token exchange failed: {e}")
        raise ORCIDAuthError(f"Failed to exchange code for token: {e}") from e


def refresh_access_token(refresh_token: str) -> dict[str, Any]:
    """
    Refresh an expired access token.

    Args:
        refresh_token: The refresh token.

    Returns:
        Dictionary containing new access_token, refresh_token, and expires_in.

    Raises:
        ORCIDAuthError: If token refresh fails.
    """
    if not is_orcid_configured():
        raise ORCIDError("ORCID integration is not configured.")

    urls = get_orcid_urls()
    token_url = f"{urls['auth_url']}/token"

    data = {
        "client_id": config.ORCID_CLIENT_ID,
        "client_secret": config.ORCID_CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }

    try:
        response = requests.post(
            token_url,
            data=data,
            headers={"Accept": "application/json"},
            timeout=ORCID_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"ORCID token refresh failed: {e}")
        raise ORCIDAuthError(f"Failed to refresh token: {e}") from e


def fetch_orcid_record(
    orcid_id: str, access_token: str | None = None
) -> dict[str, Any]:
    """
    Fetch full ORCID record for a researcher.

    Args:
        orcid_id: ORCID identifier (e.g., "0000-0002-1825-0097").
        access_token: Optional access token for authenticated requests.

    Returns:
        ORCID record as dictionary.

    Raises:
        ORCIDAPIError: If API request fails.
    """
    urls = get_orcid_urls()
    record_url = f"{urls['api_url']}/{orcid_id}/record"

    headers = {"Accept": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"

    try:
        response = requests.get(
            record_url,
            headers=headers,
            timeout=ORCID_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch ORCID record {orcid_id}: {e}")
        raise ORCIDAPIError(f"Failed to fetch ORCID record: {e}") from e


def fetch_orcid_works(
    orcid_id: str, access_token: str | None = None
) -> list[dict[str, Any]]:
    """
    Fetch publications (works) from ORCID profile.

    Args:
        orcid_id: ORCID identifier.
        access_token: Optional access token for authenticated requests.

    Returns:
        List of work records.

    Raises:
        ORCIDAPIError: If API request fails.
    """
    urls = get_orcid_urls()
    works_url = f"{urls['api_url']}/{orcid_id}/works"

    headers = {"Accept": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"

    try:
        response = requests.get(
            works_url,
            headers=headers,
            timeout=ORCID_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        return data.get("group", [])
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch ORCID works for {orcid_id}: {e}")
        raise ORCIDAPIError(f"Failed to fetch ORCID works: {e}") from e


def fetch_orcid_employments(
    orcid_id: str, access_token: str | None = None
) -> list[dict[str, Any]]:
    """
    Fetch employment affiliations from ORCID profile.

    Args:
        orcid_id: ORCID identifier.
        access_token: Optional access token for authenticated requests.

    Returns:
        List of employment records.

    Raises:
        ORCIDAPIError: If API request fails.
    """
    urls = get_orcid_urls()
    url = f"{urls['api_url']}/{orcid_id}/employments"

    headers = {"Accept": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"

    try:
        response = requests.get(
            url,
            headers=headers,
            timeout=ORCID_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        return data.get("affiliation-group", [])
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch ORCID employments for {orcid_id}: {e}")
        raise ORCIDAPIError(f"Failed to fetch ORCID employments: {e}") from e


def fetch_orcid_educations(
    orcid_id: str, access_token: str | None = None
) -> list[dict[str, Any]]:
    """
    Fetch education affiliations from ORCID profile.

    Args:
        orcid_id: ORCID identifier.
        access_token: Optional access token for authenticated requests.

    Returns:
        List of education records.

    Raises:
        ORCIDAPIError: If API request fails.
    """
    urls = get_orcid_urls()
    url = f"{urls['api_url']}/{orcid_id}/educations"

    headers = {"Accept": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"

    try:
        response = requests.get(
            url,
            headers=headers,
            timeout=ORCID_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        return data.get("affiliation-group", [])
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch ORCID educations for {orcid_id}: {e}")
        raise ORCIDAPIError(f"Failed to fetch ORCID educations: {e}") from e


def parse_orcid_date(date_dict: dict | None) -> str | None:
    """
    Parse ORCID date format to ISO date string.

    Args:
        date_dict: ORCID date dictionary with year, month, day keys.

    Returns:
        ISO date string (YYYY-MM-DD) or None.
    """
    if not date_dict:
        return None

    year = date_dict.get("year", {}).get("value")
    if not year:
        return None

    month = date_dict.get("month", {}).get("value") or "01"
    day = date_dict.get("day", {}).get("value") or "01"

    return f"{year}-{month.zfill(2)}-{day.zfill(2)}"


def map_orcid_work_type(work_type: str | None) -> str:
    """
    Map ORCID work type to Waldur publication venue type.

    Args:
        work_type: ORCID work type string.

    Returns:
        Waldur PublicationVenueTypes value.
    """
    type_mapping = {
        "journal-article": PublicationVenueTypes.JOURNAL,
        "conference-paper": PublicationVenueTypes.CONFERENCE,
        "conference-abstract": PublicationVenueTypes.CONFERENCE,
        "conference-poster": PublicationVenueTypes.CONFERENCE,
        "book": PublicationVenueTypes.BOOK,
        "book-chapter": PublicationVenueTypes.BOOK,
        "dissertation": PublicationVenueTypes.THESIS,
        "dissertation-thesis": PublicationVenueTypes.THESIS,
        "preprint": PublicationVenueTypes.PREPRINT,
        "report": PublicationVenueTypes.REPORT,
        "working-paper": PublicationVenueTypes.REPORT,
    }
    return type_mapping.get(work_type or "", PublicationVenueTypes.OTHER)


def import_orcid_works(profile) -> dict[str, int]:
    """
    Import publications from ORCID to reviewer profile.

    Args:
        profile: ReviewerProfile instance with orcid_id and access token.

    Returns:
        Dictionary with counts: {"imported": N, "updated": M, "skipped": K}.

    Raises:
        ORCIDError: If ORCID ID is not set or import fails.
    """
    from waldur_mastermind.proposal.models import ReviewerPublication

    if not profile.orcid_id:
        raise ORCIDError("Profile does not have an ORCID ID.")

    works = fetch_orcid_works(profile.orcid_id, profile.orcid_access_token)

    stats = {"imported": 0, "updated": 0, "skipped": 0}

    for work_group in works:
        summaries = work_group.get("work-summary", [])
        if not summaries:
            continue

        # Use the first summary (preferred source)
        summary = summaries[0]

        title = summary.get("title", {}).get("title", {}).get("value", "")
        if not title:
            stats["skipped"] += 1
            continue

        # Extract DOI if available
        doi = None
        for ext_id in summary.get("external-ids", {}).get("external-id", []):
            if ext_id.get("external-id-type") == "doi":
                doi = ext_id.get("external-id-value")
                break

        # Extract publication year
        pub_date = summary.get("publication-date")
        pub_year = None
        if pub_date and pub_date.get("year"):
            try:
                pub_year = int(pub_date["year"]["value"])
            except (ValueError, KeyError):
                pass

        # Extract venue
        venue = (
            summary.get("journal-title", {}).get("value", "")
            if summary.get("journal-title")
            else ""
        )

        # Determine venue type
        work_type = summary.get("type")
        venue_type = map_orcid_work_type(work_type)

        # Extract external IDs
        external_ids = {}
        for ext_id in summary.get("external-ids", {}).get("external-id", []):
            id_type = ext_id.get("external-id-type")
            id_value = ext_id.get("external-id-value")
            if id_type and id_value:
                external_ids[id_type] = id_value

        # Check if publication already exists (by DOI or title)
        existing = None
        if doi:
            existing = profile.publications.filter(doi=doi).first()
        if not existing:
            existing = profile.publications.filter(
                title__iexact=title, publication_year=pub_year
            ).first()

        if existing:
            # Update existing publication
            existing.venue = venue or existing.venue
            existing.venue_type = venue_type
            existing.external_ids = external_ids
            existing.save()
            stats["updated"] += 1
        else:
            # Create new publication
            ReviewerPublication.objects.create(
                reviewer_profile=profile,
                title=title,
                doi=doi,
                publication_year=pub_year,
                venue=venue,
                venue_type=venue_type,
                external_ids=external_ids,
                coauthors=[],  # Would need to fetch full work record for coauthors
            )
            stats["imported"] += 1

    # Update last sync time
    profile.orcid_last_sync = timezone.now()
    profile.save(update_fields=["orcid_last_sync"])

    return stats


def import_orcid_affiliations(profile) -> dict[str, int]:
    """
    Import affiliations (employment and education) from ORCID to reviewer profile.

    Args:
        profile: ReviewerProfile instance with orcid_id and access token.

    Returns:
        Dictionary with counts: {"imported": N, "updated": M, "skipped": K}.

    Raises:
        ORCIDError: If ORCID ID is not set or import fails.
    """
    from waldur_mastermind.proposal.models import ReviewerAffiliation

    if not profile.orcid_id:
        raise ORCIDError("Profile does not have an ORCID ID.")

    stats = {"imported": 0, "updated": 0, "skipped": 0}

    # Fetch employments
    employments = fetch_orcid_employments(profile.orcid_id, profile.orcid_access_token)
    for emp_group in employments:
        for summary in emp_group.get("summaries", []):
            employment = summary.get("employment-summary", {})
            org_name = employment.get("organization", {}).get("name", "")
            if not org_name:
                stats["skipped"] += 1
                continue

            department = employment.get("department-name", "")
            role = employment.get("role-title", "")
            start_date = parse_orcid_date(employment.get("start-date"))
            end_date = parse_orcid_date(employment.get("end-date"))

            # Extract organization identifiers
            org_identifier = None
            disambig = employment.get("organization", {}).get(
                "disambiguated-organization"
            )
            if disambig:
                org_identifier = disambig.get("disambiguated-organization-identifier")

            # Check if affiliation already exists
            existing = profile.affiliations.filter(
                organization_name__iexact=org_name,
                affiliation_type=ReviewerAffiliationTypes.EMPLOYMENT,
            ).first()

            if existing:
                existing.department = department or existing.department
                existing.position_title = role or existing.position_title
                existing.organization_identifier = (
                    org_identifier or existing.organization_identifier
                )
                if start_date:
                    existing.start_date = start_date
                if end_date:
                    existing.end_date = end_date
                existing.save()
                stats["updated"] += 1
            else:
                ReviewerAffiliation.objects.create(
                    reviewer_profile=profile,
                    organization_name=org_name,
                    organization_identifier=org_identifier or "",
                    department=department or "",
                    position_title=role or "",
                    start_date=start_date,
                    end_date=end_date,
                    affiliation_type=ReviewerAffiliationTypes.EMPLOYMENT,
                )
                stats["imported"] += 1

    # Fetch educations
    educations = fetch_orcid_educations(profile.orcid_id, profile.orcid_access_token)
    for edu_group in educations:
        for summary in edu_group.get("summaries", []):
            education = summary.get("education-summary", {})
            org_name = education.get("organization", {}).get("name", "")
            if not org_name:
                stats["skipped"] += 1
                continue

            department = education.get("department-name", "")
            role = education.get("role-title", "")  # Degree name
            start_date = parse_orcid_date(education.get("start-date"))
            end_date = parse_orcid_date(education.get("end-date"))

            org_identifier = None
            disambig = education.get("organization", {}).get(
                "disambiguated-organization"
            )
            if disambig:
                org_identifier = disambig.get("disambiguated-organization-identifier")

            existing = profile.affiliations.filter(
                organization_name__iexact=org_name,
                affiliation_type=ReviewerAffiliationTypes.EDUCATION,
            ).first()

            if existing:
                existing.department = department or existing.department
                existing.position_title = role or existing.position_title
                existing.organization_identifier = (
                    org_identifier or existing.organization_identifier
                )
                if start_date:
                    existing.start_date = start_date
                if end_date:
                    existing.end_date = end_date
                existing.save()
                stats["updated"] += 1
            else:
                ReviewerAffiliation.objects.create(
                    reviewer_profile=profile,
                    organization_name=org_name,
                    organization_identifier=org_identifier or "",
                    department=department or "",
                    position_title=role or "",
                    start_date=start_date,
                    end_date=end_date,
                    affiliation_type=ReviewerAffiliationTypes.EDUCATION,
                )
                stats["imported"] += 1

    return stats


def sync_orcid_profile(profile) -> dict[str, Any]:
    """
    Synchronize all data from ORCID to reviewer profile.

    This imports works and affiliations from ORCID.

    Args:
        profile: ReviewerProfile instance.

    Returns:
        Dictionary with sync results.

    Raises:
        ORCIDError: If sync fails.
    """
    if not profile.orcid_id:
        raise ORCIDError("Profile does not have an ORCID ID.")

    results = {
        "works": {"imported": 0, "updated": 0, "skipped": 0},
        "affiliations": {"imported": 0, "updated": 0, "skipped": 0},
        "errors": [],
    }

    try:
        results["works"] = import_orcid_works(profile)
    except ORCIDError as e:
        results["errors"].append(f"Works import failed: {e}")
        logger.error(f"Failed to import ORCID works for {profile.orcid_id}: {e}")

    try:
        results["affiliations"] = import_orcid_affiliations(profile)
    except ORCIDError as e:
        results["errors"].append(f"Affiliations import failed: {e}")
        logger.error(f"Failed to import ORCID affiliations for {profile.orcid_id}: {e}")

    # Update last sync timestamp
    profile.orcid_last_sync = timezone.now()
    profile.save(update_fields=["orcid_last_sync"])

    return results


def connect_orcid_to_profile(profile, code: str) -> dict[str, Any]:
    """
    Complete ORCID OAuth flow and connect ORCID to reviewer profile.

    Args:
        profile: ReviewerProfile instance.
        code: Authorization code from OAuth callback.

    Returns:
        Dictionary with connection results including orcid_id.

    Raises:
        ORCIDAuthError: If OAuth fails.
    """
    token_data = exchange_code_for_token(code)

    orcid_id = token_data.get("orcid")
    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")
    expires_in = token_data.get("expires_in", 631138518)  # ~20 years default

    if not orcid_id or not access_token:
        raise ORCIDAuthError("Invalid token response from ORCID.")

    # Update profile with ORCID credentials
    profile.orcid_id = orcid_id
    profile.orcid_access_token = access_token
    profile.orcid_refresh_token = refresh_token
    profile.orcid_token_expires = timezone.now() + timedelta(seconds=expires_in)
    profile.save()

    return {
        "orcid_id": orcid_id,
        "connected": True,
    }


def disconnect_orcid_from_profile(profile) -> None:
    """
    Disconnect ORCID from reviewer profile.

    This clears all ORCID-related data including the ORCID ID.

    Args:
        profile: ReviewerProfile instance.
    """
    profile.orcid_id = ""
    profile.orcid_access_token = ""
    profile.orcid_refresh_token = ""
    profile.orcid_last_sync = None
    profile.save(
        update_fields=[
            "orcid_id",
            "orcid_access_token",
            "orcid_refresh_token",
            "orcid_last_sync",
        ]
    )


def fetch_orcid_keywords(orcid_id: str, access_token: str | None = None) -> list[str]:
    """
    Fetch research keywords from ORCID profile.

    Args:
        orcid_id: ORCID identifier.
        access_token: Optional access token for authenticated requests.

    Returns:
        List of keyword strings.

    Raises:
        ORCIDAPIError: If API request fails.
    """
    urls = get_orcid_urls()
    url = f"{urls['api_url']}/{orcid_id}/keywords"

    headers = {"Accept": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"

    try:
        response = requests.get(
            url,
            headers=headers,
            timeout=ORCID_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        keywords = []
        for keyword_item in data.get("keyword", []):
            content = keyword_item.get("content")
            if content:
                keywords.append(content)

        return keywords
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch ORCID keywords for {orcid_id}: {e}")
        raise ORCIDAPIError(f"Failed to fetch ORCID keywords: {e}") from e


def import_orcid_keywords(profile) -> dict[str, int]:
    """
    Import research keywords from ORCID as expertise entries.

    Args:
        profile: ReviewerProfile instance with orcid_id and access token.

    Returns:
        Dictionary with counts: {"imported": N, "skipped": K}.

    Raises:
        ORCIDError: If ORCID ID is not set or import fails.
    """
    from waldur_mastermind.proposal.enums import ExpertiseProficiencyLevels
    from waldur_mastermind.proposal.models import ReviewerExpertise

    if not profile.orcid_id:
        raise ORCIDError("Profile does not have an ORCID ID.")

    keywords = fetch_orcid_keywords(profile.orcid_id, profile.orcid_access_token)

    stats = {"imported": 0, "skipped": 0}

    for keyword in keywords:
        keyword = keyword.strip()
        if not keyword:
            stats["skipped"] += 1
            continue

        # Check if expertise already exists
        existing = profile.expertise_set.filter(
            expertise_keyword__iexact=keyword
        ).first()

        if existing:
            stats["skipped"] += 1
        else:
            ReviewerExpertise.objects.create(
                reviewer_profile=profile,
                expertise_keyword=keyword,
                proficiency_level=ExpertiseProficiencyLevels.FAMILIAR,
            )
            stats["imported"] += 1

    return stats


def fetch_publication_by_doi(doi: str) -> dict[str, Any] | None:
    """
    Fetch publication metadata by DOI using CrossRef API.

    Args:
        doi: The DOI to look up (e.g., "10.1000/xyz123").

    Returns:
        Dictionary with publication data or None if not found.

    Raises:
        ORCIDAPIError: If API request fails.
    """
    from constance import config

    # Clean up DOI
    doi = doi.strip()
    if doi.startswith("https://doi.org/"):
        doi = doi[16:]
    elif doi.startswith("http://doi.org/"):
        doi = doi[15:]
    elif doi.startswith("doi:"):
        doi = doi[4:]

    url = f"https://api.crossref.org/works/{doi}"

    headers = {"Accept": "application/json"}
    # Add polite pool email if configured
    if config.CROSSREF_MAILTO:
        headers["User-Agent"] = f"Waldur (mailto:{config.CROSSREF_MAILTO})"

    try:
        response = requests.get(
            url,
            headers=headers,
            timeout=ORCID_REQUEST_TIMEOUT,
        )

        if response.status_code == 404:
            return None

        response.raise_for_status()
        data = response.json()

        work = data.get("message", {})

        # Extract title
        titles = work.get("title", [])
        title = titles[0] if titles else ""

        # Extract publication year
        pub_year = None
        date_parts = work.get("published-print", {}).get("date-parts", [[]])
        if not date_parts or not date_parts[0]:
            date_parts = work.get("published-online", {}).get("date-parts", [[]])
        if date_parts and date_parts[0]:
            pub_year = date_parts[0][0]

        # Extract venue
        venues = work.get("container-title", [])
        venue = venues[0] if venues else ""

        # Extract abstract
        abstract = work.get("abstract", "")

        # Determine venue type
        work_type = work.get("type", "")
        type_mapping = {
            "journal-article": PublicationVenueTypes.JOURNAL,
            "proceedings-article": PublicationVenueTypes.CONFERENCE,
            "book": PublicationVenueTypes.BOOK,
            "book-chapter": PublicationVenueTypes.BOOK,
            "dissertation": PublicationVenueTypes.THESIS,
            "report": PublicationVenueTypes.REPORT,
            "posted-content": PublicationVenueTypes.PREPRINT,
        }
        venue_type = type_mapping.get(work_type, PublicationVenueTypes.OTHER)

        # Extract coauthors
        coauthors = []
        for author in work.get("author", []):
            name_parts = []
            if author.get("given"):
                name_parts.append(author["given"])
            if author.get("family"):
                name_parts.append(author["family"])

            coauthor = {"name": " ".join(name_parts)}
            if author.get("ORCID"):
                # Extract ORCID ID from URL
                orcid_url = author["ORCID"]
                if "/" in orcid_url:
                    coauthor["orcid"] = orcid_url.split("/")[-1]

            coauthors.append(coauthor)

        return {
            "title": title,
            "publication_year": pub_year,
            "venue": venue,
            "venue_type": venue_type,
            "abstract": abstract[:2000] if abstract else "",  # Truncate long abstracts
            "coauthors": coauthors,
            "external_ids": {"doi": doi},
        }

    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch publication by DOI {doi}: {e}")
        raise ORCIDAPIError(f"Failed to fetch publication: {e}") from e
