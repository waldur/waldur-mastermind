"""
Affinity Scoring Service for Reviewer-Proposal Matching.

This module provides algorithms for computing expertise affinity scores
between reviewers and proposals for optimal review assignment.
"""

import logging
import math
import re
from collections import Counter
from datetime import date

from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import (
    AffinityMatrixScopes,
    AffinityMatrixSources,
    ExpertiseProficiencyLevels,
    MatchingAffinityMethods,
)

logger = logging.getLogger(__name__)


def tokenize(text: str) -> list[str]:
    """
    Tokenize text into lowercase words, removing punctuation.
    """
    if not text:
        return []
    # Convert to lowercase and extract words
    words = re.findall(r"\b[a-z]+\b", text.lower())
    # Remove common stopwords
    stopwords = {
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "as",
        "is",
        "was",
        "are",
        "were",
        "been",
        "be",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "must",
        "shall",
        "can",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "we",
        "our",
        "they",
        "their",
    }
    return [w for w in words if w not in stopwords and len(w) > 2]


def compute_tf(tokens: list[str]) -> dict[str, float]:
    """Compute term frequency for a list of tokens."""
    if not tokens:
        return {}
    counts = Counter(tokens)
    total = len(tokens)
    return {term: count / total for term, count in counts.items()}


def compute_idf(documents: list[list[str]]) -> dict[str, float]:
    """Compute inverse document frequency across a corpus."""
    if not documents:
        return {}

    n_docs = len(documents)
    doc_freq = Counter()

    for doc in documents:
        unique_terms = set(doc)
        doc_freq.update(unique_terms)

    return {term: math.log(n_docs / (1 + freq)) for term, freq in doc_freq.items()}


def compute_tfidf_vector(tokens: list[str], idf: dict[str, float]) -> dict[str, float]:
    """Compute TF-IDF vector for a document."""
    tf = compute_tf(tokens)
    return {term: tf_val * idf.get(term, 0) for term, tf_val in tf.items()}


def cosine_similarity(vec1: dict[str, float], vec2: dict[str, float]) -> float:
    """Compute cosine similarity between two sparse vectors."""
    if not vec1 or not vec2:
        return 0.0

    # Get common terms
    common_terms = set(vec1.keys()) & set(vec2.keys())
    if not common_terms:
        return 0.0

    # Compute dot product
    dot_product = sum(vec1[term] * vec2[term] for term in common_terms)

    # Compute magnitudes
    mag1 = math.sqrt(sum(v**2 for v in vec1.values()))
    mag2 = math.sqrt(sum(v**2 for v in vec2.values()))

    if mag1 == 0 or mag2 == 0:
        return 0.0

    return dot_product / (mag1 * mag2)


def get_reviewer_text(reviewer: "models.ReviewerProfile") -> str:
    """
    Get combined text from reviewer's publications and expertise.
    """
    texts = []

    # Add expertise keywords with weight
    for expertise in reviewer.expertise_set.all():
        keyword = expertise.expertise_keyword
        # Weight by proficiency level
        if expertise.proficiency_level == ExpertiseProficiencyLevels.EXPERT:
            texts.extend([keyword] * 3)
        elif expertise.proficiency_level == ExpertiseProficiencyLevels.FAMILIAR:
            texts.extend([keyword] * 2)
        else:
            texts.append(keyword)

    # Add recent publication titles and abstracts
    cutoff_year = date.today().year - 5
    publications = reviewer.publications.filter(
        publication_year__gte=cutoff_year,
        is_excluded_from_matching=False,
    )

    for pub in publications:
        texts.append(pub.title)
        if pub.abstract:
            texts.append(pub.abstract)

    # Add biography
    if reviewer.biography:
        texts.append(reviewer.biography)

    return " ".join(texts)


def get_proposal_text(proposal: "models.Proposal") -> str:
    """
    Get combined text from proposal for matching.
    """
    texts = []

    if proposal.name:
        texts.append(proposal.name)

    # Add project_summary directly from proposal
    if proposal.project_summary:
        texts.append(proposal.project_summary)

    # Also check projectindication for backwards compatibility
    if hasattr(proposal, "projectindication"):
        indication = proposal.projectindication
        if hasattr(indication, "project_summary") and indication.project_summary:
            texts.append(indication.project_summary)

    # Add description if available
    if proposal.description:
        texts.append(proposal.description)

    return " ".join(texts)


def get_call_text(call: "models.Call") -> str:
    """
    Get combined text from call for matching (early stage when no proposals exist).
    """
    texts = []

    if call.name:
        texts.append(call.name)

    if call.description:
        texts.append(call.description)

    return " ".join(texts)


def compute_keyword_match_details(
    reviewer: "models.ReviewerProfile",
    target_text: str,
) -> dict:
    """
    Compute keyword matching with details about which keywords matched.

    Args:
        reviewer: The reviewer profile to check
        target_text: Text to match keywords against (proposal, call description, or custom)

    Returns:
        Dictionary with:
        - score: Float 0-1 affinity score
        - matched_keywords: List of keywords that matched
        - total_keywords: Total reviewer keywords checked
    """
    reviewer_keywords = set()
    keyword_weights = {}

    for expertise in reviewer.expertise_set.filter(
        proficiency_level__in=[
            ExpertiseProficiencyLevels.EXPERT,
            ExpertiseProficiencyLevels.FAMILIAR,
        ]
    ):
        keyword = expertise.expertise_keyword.lower().strip()
        reviewer_keywords.add(keyword)
        if expertise.proficiency_level == ExpertiseProficiencyLevels.EXPERT:
            keyword_weights[keyword] = 1.0
        else:
            keyword_weights[keyword] = 0.7

    if not reviewer_keywords:
        return {
            "score": 0.0,
            "matched_keywords": [],
            "total_keywords": 0,
        }

    target_text_lower = target_text.lower() if target_text else ""

    if not target_text_lower:
        return {
            "score": 0.0,
            "matched_keywords": [],
            "total_keywords": len(reviewer_keywords),
        }

    # Find matches
    matched = []
    total_weight = 0.0
    max_possible_weight = sum(keyword_weights.values())

    for keyword in reviewer_keywords:
        if keyword in target_text_lower:
            matched.append(keyword)
            total_weight += keyword_weights.get(keyword, 0.5)

    score = (
        min(1.0, total_weight / max_possible_weight) if max_possible_weight > 0 else 0.0
    )

    return {
        "score": score,
        "matched_keywords": sorted(matched),
        "total_keywords": len(reviewer_keywords),
    }


def compute_affinity_against_text(
    reviewer: "models.ReviewerProfile",
    target_text: str,
    config: "models.MatchingConfiguration | None" = None,
    corpus_idf: dict[str, float] | None = None,
) -> dict[str, float | list]:
    """
    Compute affinity between a reviewer and arbitrary target text.

    This is used for matching against call description or custom keywords.

    Returns dictionary with:
    - affinity_score: Combined score (0-1)
    - keyword_score: Keyword-based score
    - text_score: TF-IDF based score
    - matched_keywords: List of matched keywords
    """
    # Get weights from config or use defaults
    if config:
        method = config.affinity_method
        keyword_weight = config.keyword_weight
        text_weight = config.text_weight
    else:
        method = MatchingAffinityMethods.COMBINED
        keyword_weight = 0.4
        text_weight = 0.6

    keyword_result = {"score": 0.0, "matched_keywords": []}
    text_score = 0.0

    if method in [MatchingAffinityMethods.KEYWORD, MatchingAffinityMethods.COMBINED]:
        keyword_result = compute_keyword_match_details(reviewer, target_text)

    if method in [MatchingAffinityMethods.TFIDF, MatchingAffinityMethods.COMBINED]:
        # Compute TF-IDF between reviewer text and target text
        reviewer_text = get_reviewer_text(reviewer)
        if reviewer_text and target_text:
            reviewer_tokens = tokenize(reviewer_text)
            target_tokens = tokenize(target_text)

            if reviewer_tokens and target_tokens:
                if corpus_idf is None:
                    corpus_idf = compute_idf([reviewer_tokens, target_tokens])

                reviewer_vec = compute_tfidf_vector(reviewer_tokens, corpus_idf)
                target_vec = compute_tfidf_vector(target_tokens, corpus_idf)
                text_score = cosine_similarity(reviewer_vec, target_vec)

    # Compute combined score
    keyword_score = keyword_result["score"]
    if method == MatchingAffinityMethods.KEYWORD:
        affinity_score = keyword_score
    elif method == MatchingAffinityMethods.TFIDF:
        affinity_score = text_score
    else:  # COMBINED
        affinity_score = keyword_weight * keyword_score + text_weight * text_score

    return {
        "affinity_score": round(affinity_score, 4),
        "keyword_score": round(keyword_score, 4) if keyword_score else None,
        "text_score": round(text_score, 4) if text_score else None,
        "matched_keywords": keyword_result["matched_keywords"],
    }


def compute_keyword_affinity(
    reviewer: "models.ReviewerProfile",
    proposal: "models.Proposal",
) -> float:
    """
    Compute affinity based on keyword overlap.

    Returns score 0-1 based on:
    - Exact keyword matches (high weight)
    - Partial keyword matches (lower weight)

    This is a convenience wrapper around compute_keyword_match_details
    that only returns the score.
    """
    proposal_text = get_proposal_text(proposal)
    result = compute_keyword_match_details(reviewer, proposal_text)
    return result["score"]


def compute_tfidf_affinity(
    reviewer: "models.ReviewerProfile",
    proposal: "models.Proposal",
    corpus_idf: dict[str, float] | None = None,
) -> float:
    """
    Compute affinity based on TF-IDF text similarity.
    """
    reviewer_text = get_reviewer_text(reviewer)
    proposal_text = get_proposal_text(proposal)

    if not reviewer_text or not proposal_text:
        return 0.0

    reviewer_tokens = tokenize(reviewer_text)
    proposal_tokens = tokenize(proposal_text)

    if not reviewer_tokens or not proposal_tokens:
        return 0.0

    # Use provided IDF or compute from both documents
    if corpus_idf is None:
        corpus_idf = compute_idf([reviewer_tokens, proposal_tokens])

    reviewer_vec = compute_tfidf_vector(reviewer_tokens, corpus_idf)
    proposal_vec = compute_tfidf_vector(proposal_tokens, corpus_idf)

    return cosine_similarity(reviewer_vec, proposal_vec)


def compute_affinity(
    reviewer: "models.ReviewerProfile",
    proposal: "models.Proposal",
    config: "models.MatchingConfiguration | None" = None,
    corpus_idf: dict[str, float] | None = None,
) -> dict[str, float]:
    """
    Compute combined affinity score between reviewer and proposal.

    Returns dictionary with:
    - affinity_score: Combined score (0-1)
    - keyword_score: Keyword-based score
    - text_score: TF-IDF based score
    """
    # Get weights from config or use defaults
    if config:
        method = config.affinity_method
        keyword_weight = config.keyword_weight
        text_weight = config.text_weight
    else:
        method = MatchingAffinityMethods.COMBINED
        keyword_weight = 0.4
        text_weight = 0.6

    keyword_score = 0.0
    text_score = 0.0

    if method in [MatchingAffinityMethods.KEYWORD, MatchingAffinityMethods.COMBINED]:
        keyword_score = compute_keyword_affinity(reviewer, proposal)

    if method in [MatchingAffinityMethods.TFIDF, MatchingAffinityMethods.COMBINED]:
        text_score = compute_tfidf_affinity(reviewer, proposal, corpus_idf)

    # Compute combined score
    if method == MatchingAffinityMethods.KEYWORD:
        affinity_score = keyword_score
    elif method == MatchingAffinityMethods.TFIDF:
        affinity_score = text_score
    else:  # COMBINED
        affinity_score = keyword_weight * keyword_score + text_weight * text_score

    return {
        "affinity_score": round(affinity_score, 4),
        "keyword_score": round(keyword_score, 4) if keyword_score else None,
        "text_score": round(text_score, 4) if text_score else None,
    }


def compute_affinities_for_call(
    call: "models.Call",
) -> list["models.ReviewerProposalAffinity"]:
    """
    Compute affinity scores for all reviewer-proposal pairs in a call.

    Returns list of created/updated ReviewerProposalAffinity objects.
    """
    from waldur_mastermind.proposal.enums import ReviewerPoolInvitationStatuses

    # Get matching configuration
    config = getattr(call, "matching_configuration", None)

    # Get active reviewers in the pool with prefetched related data
    pool_members = (
        models.CallReviewerPool.objects.filter(
            call=call,
            invitation_status=ReviewerPoolInvitationStatuses.ACCEPTED,
        )
        .select_related("reviewer", "reviewer__user")
        .prefetch_related(
            "reviewer__expertise_set",
            "reviewer__publications",
        )
    )

    reviewers = [pm.reviewer for pm in pool_members if pm.reviewer]

    # Get proposals for this call
    proposals = list(models.Proposal.objects.filter(round__call=call))

    if not reviewers or not proposals:
        return []

    # Build corpus IDF from all reviewer and proposal texts
    all_documents = []
    for reviewer in reviewers:
        text = get_reviewer_text(reviewer)
        if text:
            all_documents.append(tokenize(text))
    for proposal in proposals:
        text = get_proposal_text(proposal)
        if text:
            all_documents.append(tokenize(text))

    corpus_idf = compute_idf(all_documents) if all_documents else {}

    # Prefetch existing affinities to determine create vs update
    existing_affinities = models.ReviewerProposalAffinity.objects.filter(
        call=call,
        reviewer__in=reviewers,
        proposal__in=proposals,
    )
    existing_lookup = {
        (aff.reviewer_id, aff.proposal_id): aff for aff in existing_affinities
    }

    # Compute all affinities and prepare for bulk operations
    to_create = []
    to_update = []

    for reviewer in reviewers:
        for proposal in proposals:
            scores = compute_affinity(reviewer, proposal, config, corpus_idf)

            existing = existing_lookup.get((reviewer.id, proposal.id))
            if existing:
                # Update existing record
                existing.affinity_score = scores["affinity_score"]
                existing.keyword_score = scores["keyword_score"]
                existing.text_score = scores["text_score"]
                to_update.append(existing)
            else:
                # Create new record
                to_create.append(
                    models.ReviewerProposalAffinity(
                        call=call,
                        reviewer=reviewer,
                        proposal=proposal,
                        affinity_score=scores["affinity_score"],
                        keyword_score=scores["keyword_score"],
                        text_score=scores["text_score"],
                    )
                )

    # Perform bulk operations
    created = []
    if to_create:
        created = models.ReviewerProposalAffinity.objects.bulk_create(to_create)

    if to_update:
        models.ReviewerProposalAffinity.objects.bulk_update(
            to_update,
            fields=["affinity_score", "keyword_score", "text_score"],
        )

    return created + to_update


def compute_suggestions_for_call(call: "models.Call") -> dict:
    """
    Compute suggestions from ALL published profiles (not just pool members).

    This function:
    1. Finds all published, available reviewers
    2. Excludes those already in the pool or already suggested
    3. Computes affinity scores between reviewers and call proposals
    4. Creates ReviewerSuggestion records for high-affinity matches

    Returns:
        dict with:
        - suggestions_created: number of new suggestions
        - reviewers_evaluated: number of reviewers evaluated
        - suggestions: list of created suggestion UUIDs
    """
    from waldur_mastermind.proposal.enums import ReviewerSuggestionStatuses

    # Get matching configuration
    config = getattr(call, "matching_configuration", None)

    # Get published, available reviewers
    reviewers = (
        models.ReviewerProfile.objects.filter(
            is_published=True,
            available_for_reviews=True,
        )
        .exclude(
            # Exclude those already in pool
            pool_memberships__call=call
        )
        .exclude(
            # Exclude those already suggested
            suggestions__call=call
        )
        .prefetch_related("expertise_set", "publications")
    )

    # Get proposals for this call
    proposals = list(models.Proposal.objects.filter(round__call=call))

    if not reviewers.exists() or not proposals:
        return {
            "suggestions_created": 0,
            "reviewers_evaluated": 0,
            "suggestions": [],
        }

    # Build corpus IDF from all reviewer and proposal texts
    all_documents = []
    reviewer_list = list(reviewers)

    for reviewer in reviewer_list:
        text = get_reviewer_text(reviewer)
        if text:
            all_documents.append(tokenize(text))
    for proposal in proposals:
        text = get_proposal_text(proposal)
        if text:
            all_documents.append(tokenize(text))

    corpus_idf = compute_idf(all_documents) if all_documents else {}

    # Compute affinities and create suggestions
    suggestions_created = []

    for reviewer in reviewer_list:
        # Compute average affinity across all proposals
        total_affinity = 0.0
        keyword_total = 0.0
        text_total = 0.0

        for proposal in proposals:
            scores = compute_affinity(reviewer, proposal, config, corpus_idf)
            total_affinity += scores["affinity_score"]
            if scores["keyword_score"]:
                keyword_total += scores["keyword_score"]
            if scores["text_score"]:
                text_total += scores["text_score"]

        avg_affinity = total_affinity / len(proposals) if proposals else 0.0
        avg_keyword = keyword_total / len(proposals) if proposals else None
        avg_text = text_total / len(proposals) if proposals else None

        # Only suggest reviewers with meaningful affinity (> 0.1)
        min_threshold = 0.1
        if config and hasattr(config, "min_affinity_threshold"):
            min_threshold = config.min_affinity_threshold

        if avg_affinity >= min_threshold:
            suggestion = models.ReviewerSuggestion.objects.create(
                call=call,
                reviewer=reviewer,
                affinity_score=round(avg_affinity, 4),
                keyword_score=round(avg_keyword, 4) if avg_keyword else None,
                text_score=round(avg_text, 4) if avg_text else None,
                status=ReviewerSuggestionStatuses.PENDING,
            )
            suggestions_created.append(str(suggestion.uuid))

    return {
        "suggestions_created": len(suggestions_created),
        "reviewers_evaluated": len(reviewer_list),
        "suggestions": suggestions_created,
    }


def get_affinity_matrix(
    call: "models.Call", scope: str = AffinityMatrixScopes.POOL
) -> dict:
    """
    Get affinity scores for a call as a flat list of reviewer-proposal pairs.

    Args:
        call: The call to get affinities for
        scope: Filter by reviewer type (use AffinityMatrixScopes enum values):
            - POOL: Only pool members (accepted reviewers) - uses cached affinities
            - SUGGESTIONS: Only suggested reviewers - computes on-the-fly
            - ALL: Both pool and suggestions

    Includes COI status if conflicts have been detected.

    Returns:
    {
        "results": [
            {
                "uuid": ...,
                "reviewer_uuid": ...,
                "reviewer_name": ...,
                "proposal_uuid": ...,
                "proposal_name": ...,
                "affinity_score": ...,
                "keyword_score": ...,
                "text_score": ...,
                "has_conflict": bool,
                "coi_type": str or None,
                "coi_severity": str or None,
                "coi_status": str or None,
                "source": str (AffinityMatrixSources value),
            },
            ...
        ],
        "count": ...
    }
    """
    from waldur_mastermind.proposal.enums import ReviewerSuggestionStatuses

    results = []

    # Build COI lookup indexed by (reviewer_id, proposal_id)
    conflicts = models.ConflictOfInterest.objects.filter(call=call)
    coi_lookup = {(coi.reviewer_id, coi.proposal_id): coi for coi in conflicts}

    # Get proposals for this call
    proposals = list(models.Proposal.objects.filter(round__call=call))

    # Get matching configuration
    config = getattr(call, "matching_configuration", None)

    if scope in [AffinityMatrixScopes.POOL, AffinityMatrixScopes.ALL]:
        # Get cached affinities for pool members
        affinities = (
            models.ReviewerProposalAffinity.objects.filter(call=call)
            .select_related("reviewer__user", "proposal")
            .order_by("-affinity_score")
        )

        for aff in affinities:
            coi = coi_lookup.get((aff.reviewer_id, aff.proposal_id))
            results.append(
                {
                    "uuid": str(aff.uuid),
                    "reviewer_uuid": str(aff.reviewer.uuid),
                    "reviewer_name": aff.reviewer.user.full_name,
                    "proposal_uuid": str(aff.proposal.uuid),
                    "proposal_name": aff.proposal.name,
                    "affinity_score": aff.affinity_score,
                    "keyword_score": aff.keyword_score,
                    "text_score": aff.text_score,
                    "has_conflict": coi is not None,
                    "coi_type": coi.coi_type if coi else None,
                    "coi_severity": coi.severity if coi else None,
                    "coi_status": coi.status if coi else None,
                    "source": AffinityMatrixSources.POOL,
                }
            )

    if scope in [AffinityMatrixScopes.SUGGESTIONS, AffinityMatrixScopes.ALL]:
        # Get suggested reviewers and compute per-proposal affinities on-the-fly
        suggestions = (
            models.ReviewerSuggestion.objects.filter(
                call=call,
                status__in=[
                    ReviewerSuggestionStatuses.PENDING,
                    ReviewerSuggestionStatuses.CONFIRMED,
                ],
            )
            .select_related("reviewer__user")
            .prefetch_related("reviewer__expertise_set", "reviewer__publications")
        )

        if suggestions.exists() and proposals:
            # Build corpus IDF for consistent scoring
            all_documents = []
            suggestion_reviewers = [s.reviewer for s in suggestions]

            for reviewer in suggestion_reviewers:
                text = get_reviewer_text(reviewer)
                if text:
                    all_documents.append(tokenize(text))
            for proposal in proposals:
                text = get_proposal_text(proposal)
                if text:
                    all_documents.append(tokenize(text))

            corpus_idf = compute_idf(all_documents) if all_documents else {}

            # Compute per-proposal affinities for each suggestion
            import uuid as uuid_module

            for suggestion in suggestions:
                reviewer = suggestion.reviewer
                for proposal in proposals:
                    scores = compute_affinity(reviewer, proposal, config, corpus_idf)
                    coi = coi_lookup.get((reviewer.id, proposal.id))

                    results.append(
                        {
                            "uuid": str(
                                uuid_module.uuid4()
                            ),  # Generated UUID for display
                            "reviewer_uuid": str(reviewer.uuid),
                            "reviewer_name": reviewer.user.full_name,
                            "proposal_uuid": str(proposal.uuid),
                            "proposal_name": proposal.name,
                            "affinity_score": scores["affinity_score"],
                            "keyword_score": scores["keyword_score"],
                            "text_score": scores["text_score"],
                            "has_conflict": coi is not None,
                            "coi_type": coi.coi_type if coi else None,
                            "coi_severity": coi.severity if coi else None,
                            "coi_status": coi.status if coi else None,
                            "source": AffinityMatrixSources.SUGGESTION,
                        }
                    )

    # Sort by affinity score descending
    results.sort(key=lambda x: x["affinity_score"], reverse=True)

    return {
        "results": results,
        "count": len(results),
    }


def compute_suggestions_for_call_configurable(
    call: "models.Call",
    source: str = "all_proposals",
    proposal_uuids: list | None = None,
    keywords: list | None = None,
    keyword_search_mode: str = "expertise_only",
    min_threshold: float | None = None,
) -> dict:
    """
    Generate reviewer suggestions with configurable matching source.

    This is an enhanced version of compute_suggestions_for_call that allows
    call managers to choose what text to match reviewers against.

    Args:
        call: The call to generate suggestions for
        source: One of:
            - "call_description": Match against call name + description
            - "all_proposals": Match against all proposals (original behavior)
            - "selected_proposals": Match against specific proposals
            - "custom_keywords": Match against user-provided keywords
        proposal_uuids: List of proposal UUIDs (for selected_proposals source)
        keywords: List of keyword strings (for custom_keywords source)
        keyword_search_mode: For custom_keywords, one of:
            - "expertise_only": Match against reviewer expertise keywords
            - "full_text": TF-IDF against all reviewer content
        min_threshold: Override minimum affinity threshold (0-1)

    Returns:
        dict with:
        - suggestions_created: number of new suggestions
        - reviewers_evaluated: number of reviewers evaluated
        - source_used: the source type that was used
        - suggestions: list of created suggestion data
    """
    from waldur_mastermind.proposal.enums import (
        SuggestionSourceTypes,
    )

    # Get matching configuration
    config = getattr(call, "matching_configuration", None)

    # Determine minimum threshold
    if min_threshold is not None:
        threshold = min_threshold
    elif config and hasattr(config, "min_affinity_threshold"):
        threshold = config.min_affinity_threshold
    else:
        threshold = 0.1

    # Get published, available reviewers
    reviewers = (
        models.ReviewerProfile.objects.filter(
            is_published=True,
            available_for_reviews=True,
        )
        .exclude(
            # Exclude those already in pool
            pool_memberships__call=call
        )
        .exclude(
            # Exclude those already suggested
            suggestions__call=call
        )
        .prefetch_related("expertise_set", "publications")
    )

    reviewer_list = list(reviewers)

    if not reviewer_list:
        return {
            "suggestions_created": 0,
            "reviewers_evaluated": 0,
            "source_used": source,
            "suggestions": [],
        }

    # Build corpus IDF for consistent scoring
    all_documents = []
    for reviewer in reviewer_list:
        text = get_reviewer_text(reviewer)
        if text:
            all_documents.append(tokenize(text))

    # Handle different source types
    if source == SuggestionSourceTypes.CALL_DESCRIPTION:
        return _generate_from_call_description(
            call=call,
            reviewer_list=reviewer_list,
            all_documents=all_documents,
            config=config,
            threshold=threshold,
        )
    elif source == SuggestionSourceTypes.CUSTOM_KEYWORDS:
        return _generate_from_custom_keywords(
            call=call,
            reviewer_list=reviewer_list,
            all_documents=all_documents,
            keywords=keywords or [],
            keyword_search_mode=keyword_search_mode,
            config=config,
            threshold=threshold,
        )
    elif source == SuggestionSourceTypes.SELECTED_PROPOSALS:
        proposals = list(
            models.Proposal.objects.filter(
                round__call=call,
                uuid__in=proposal_uuids or [],
            )
        )
        if not proposals:
            return {
                "suggestions_created": 0,
                "reviewers_evaluated": len(reviewer_list),
                "source_used": source,
                "suggestions": [],
            }
        return _generate_from_proposals(
            call=call,
            reviewer_list=reviewer_list,
            proposals=proposals,
            all_documents=all_documents,
            config=config,
            threshold=threshold,
            source_type=SuggestionSourceTypes.SELECTED_PROPOSALS,
        )
    else:  # all_proposals (default)
        proposals = list(models.Proposal.objects.filter(round__call=call))
        if not proposals:
            # Fallback to call description if no proposals
            return _generate_from_call_description(
                call=call,
                reviewer_list=reviewer_list,
                all_documents=all_documents,
                config=config,
                threshold=threshold,
            )
        return _generate_from_proposals(
            call=call,
            reviewer_list=reviewer_list,
            proposals=proposals,
            all_documents=all_documents,
            config=config,
            threshold=threshold,
            source_type=SuggestionSourceTypes.ALL_PROPOSALS,
        )


def _generate_from_call_description(
    call: "models.Call",
    reviewer_list: list,
    all_documents: list,
    config,
    threshold: float,
) -> dict:
    """Generate suggestions by matching reviewers to call description."""
    from waldur_mastermind.proposal.enums import (
        ReviewerSuggestionStatuses,
        SuggestionSourceTypes,
    )

    target_text = get_call_text(call)
    if not target_text.strip():
        return {
            "suggestions_created": 0,
            "reviewers_evaluated": len(reviewer_list),
            "source_used": SuggestionSourceTypes.CALL_DESCRIPTION,
            "suggestions": [],
        }

    # Add call text to corpus
    all_documents.append(tokenize(target_text))
    corpus_idf = compute_idf(all_documents) if all_documents else {}

    suggestions_created = []

    for reviewer in reviewer_list:
        scores = compute_affinity_against_text(
            reviewer, target_text, config, corpus_idf
        )

        if scores["affinity_score"] >= threshold:
            suggestion = models.ReviewerSuggestion.objects.create(
                call=call,
                reviewer=reviewer,
                affinity_score=scores["affinity_score"],
                keyword_score=scores["keyword_score"],
                text_score=scores["text_score"],
                status=ReviewerSuggestionStatuses.PENDING,
                matched_keywords=scores.get("matched_keywords", []),
                top_matching_proposals=[],  # No proposals for call description source
                source_type=SuggestionSourceTypes.CALL_DESCRIPTION,
            )
            suggestions_created.append(
                {
                    "uuid": str(suggestion.uuid),
                    "reviewer_uuid": str(reviewer.uuid),
                    "reviewer_name": reviewer.user.full_name,
                    "affinity_score": scores["affinity_score"],
                    "keyword_score": scores["keyword_score"],
                    "text_score": scores["text_score"],
                    "matched_keywords": scores.get("matched_keywords", []),
                    "top_matching_proposals": [],
                }
            )

    return {
        "suggestions_created": len(suggestions_created),
        "reviewers_evaluated": len(reviewer_list),
        "source_used": SuggestionSourceTypes.CALL_DESCRIPTION,
        "suggestions": suggestions_created,
    }


def _generate_from_custom_keywords(
    call: "models.Call",
    reviewer_list: list,
    all_documents: list,
    keywords: list,
    keyword_search_mode: str,
    config,
    threshold: float,
) -> dict:
    """Generate suggestions by matching reviewers to custom keywords."""
    from waldur_mastermind.proposal.enums import (
        KeywordSearchModes,
        ReviewerSuggestionStatuses,
        SuggestionSourceTypes,
    )

    if not keywords:
        return {
            "suggestions_created": 0,
            "reviewers_evaluated": len(reviewer_list),
            "source_used": SuggestionSourceTypes.CUSTOM_KEYWORDS,
            "suggestions": [],
        }

    suggestions_created = []

    if keyword_search_mode == KeywordSearchModes.EXPERTISE_ONLY:
        # Exact match against reviewer expertise keywords
        keyword_set = {k.lower().strip() for k in keywords}

        for reviewer in reviewer_list:
            reviewer_keywords = {
                e.expertise_keyword.lower().strip()
                for e in reviewer.expertise_set.all()
            }
            matched = reviewer_keywords & keyword_set

            if matched:
                # Score based on match ratio
                score = len(matched) / len(keyword_set)

                if score >= threshold:
                    suggestion = models.ReviewerSuggestion.objects.create(
                        call=call,
                        reviewer=reviewer,
                        affinity_score=round(score, 4),
                        keyword_score=round(score, 4),
                        text_score=None,
                        status=ReviewerSuggestionStatuses.PENDING,
                        matched_keywords=sorted(matched),
                        top_matching_proposals=[],
                        source_type=SuggestionSourceTypes.CUSTOM_KEYWORDS,
                    )
                    suggestions_created.append(
                        {
                            "uuid": str(suggestion.uuid),
                            "reviewer_uuid": str(reviewer.uuid),
                            "reviewer_name": reviewer.user.full_name,
                            "affinity_score": round(score, 4),
                            "keyword_score": round(score, 4),
                            "text_score": None,
                            "matched_keywords": sorted(matched),
                            "top_matching_proposals": [],
                        }
                    )
    else:
        # Full text search using TF-IDF
        target_text = " ".join(keywords)
        all_documents.append(tokenize(target_text))
        corpus_idf = compute_idf(all_documents) if all_documents else {}

        for reviewer in reviewer_list:
            scores = compute_affinity_against_text(
                reviewer, target_text, config, corpus_idf
            )

            if scores["affinity_score"] >= threshold:
                suggestion = models.ReviewerSuggestion.objects.create(
                    call=call,
                    reviewer=reviewer,
                    affinity_score=scores["affinity_score"],
                    keyword_score=scores["keyword_score"],
                    text_score=scores["text_score"],
                    status=ReviewerSuggestionStatuses.PENDING,
                    matched_keywords=scores.get("matched_keywords", []),
                    top_matching_proposals=[],
                    source_type=SuggestionSourceTypes.CUSTOM_KEYWORDS,
                )
                suggestions_created.append(
                    {
                        "uuid": str(suggestion.uuid),
                        "reviewer_uuid": str(reviewer.uuid),
                        "reviewer_name": reviewer.user.full_name,
                        "affinity_score": scores["affinity_score"],
                        "keyword_score": scores["keyword_score"],
                        "text_score": scores["text_score"],
                        "matched_keywords": scores.get("matched_keywords", []),
                        "top_matching_proposals": [],
                    }
                )

    return {
        "suggestions_created": len(suggestions_created),
        "reviewers_evaluated": len(reviewer_list),
        "source_used": SuggestionSourceTypes.CUSTOM_KEYWORDS,
        "suggestions": suggestions_created,
    }


def _generate_from_proposals(
    call: "models.Call",
    reviewer_list: list,
    proposals: list,
    all_documents: list,
    config,
    threshold: float,
    source_type: str,
) -> dict:
    """Generate suggestions by matching reviewers to proposals."""
    from waldur_mastermind.proposal.enums import ReviewerSuggestionStatuses

    # Add proposal texts to corpus
    for proposal in proposals:
        text = get_proposal_text(proposal)
        if text:
            all_documents.append(tokenize(text))

    corpus_idf = compute_idf(all_documents) if all_documents else {}

    suggestions_created = []

    for reviewer in reviewer_list:
        # Compute affinity for each proposal
        proposal_scores = []

        for proposal in proposals:
            scores = compute_affinity(reviewer, proposal, config, corpus_idf)

            # Also get matched keywords
            proposal_text = get_proposal_text(proposal)
            keyword_details = compute_keyword_match_details(reviewer, proposal_text)

            proposal_scores.append(
                {
                    "uuid": str(proposal.uuid),
                    "name": proposal.name,
                    "slug": getattr(proposal, "slug", ""),
                    "affinity": scores["affinity_score"],
                    "keyword_score": scores["keyword_score"],
                    "text_score": scores["text_score"],
                    "matched_keywords": keyword_details.get("matched_keywords", []),
                }
            )

        # Sort by affinity and get top matches
        proposal_scores.sort(key=lambda x: x["affinity"], reverse=True)

        # Calculate average affinity
        total_affinity = sum(p["affinity"] for p in proposal_scores)
        avg_affinity = total_affinity / len(proposals) if proposals else 0.0

        # Average keyword and text scores
        keyword_total = sum(p["keyword_score"] or 0 for p in proposal_scores)
        text_total = sum(p["text_score"] or 0 for p in proposal_scores)
        avg_keyword = keyword_total / len(proposals) if proposals else None
        avg_text = text_total / len(proposals) if proposals else None

        # Aggregate matched keywords across proposals
        all_matched_keywords = set()
        for p in proposal_scores:
            all_matched_keywords.update(p.get("matched_keywords", []))

        if avg_affinity >= threshold:
            # Store top 5 proposals with highest affinity
            top_proposals = [
                {
                    "uuid": p["uuid"],
                    "name": p["name"],
                    "slug": p.get("slug", ""),
                    "affinity": round(p["affinity"], 4),
                }
                for p in proposal_scores[:5]
            ]

            suggestion = models.ReviewerSuggestion.objects.create(
                call=call,
                reviewer=reviewer,
                affinity_score=round(avg_affinity, 4),
                keyword_score=round(avg_keyword, 4) if avg_keyword else None,
                text_score=round(avg_text, 4) if avg_text else None,
                status=ReviewerSuggestionStatuses.PENDING,
                matched_keywords=sorted(all_matched_keywords),
                top_matching_proposals=top_proposals,
                source_type=source_type,
            )
            suggestions_created.append(
                {
                    "uuid": str(suggestion.uuid),
                    "reviewer_uuid": str(reviewer.uuid),
                    "reviewer_name": reviewer.user.full_name,
                    "affinity_score": round(avg_affinity, 4),
                    "keyword_score": round(avg_keyword, 4) if avg_keyword else None,
                    "text_score": round(avg_text, 4) if avg_text else None,
                    "matched_keywords": sorted(all_matched_keywords),
                    "top_matching_proposals": top_proposals,
                }
            )

    return {
        "suggestions_created": len(suggestions_created),
        "reviewers_evaluated": len(reviewer_list),
        "source_used": source_type,
        "suggestions": suggestions_created,
    }
