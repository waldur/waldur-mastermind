from django.db import models


class CallStates(models.TextChoices):
    DRAFT = "draft", "Draft"
    ACTIVE = "active", "Active"
    ARCHIVED = "archived", "Archived"


class RoundStatuses(models.TextChoices):
    """Status values for proposal rounds."""

    SCHEDULED = "scheduled", "Round is scheduled"
    OPEN = "open", "Round is open"
    ENDED = "ended", "Round is ended"


class RequestedOfferingStates(models.TextChoices):
    REQUESTED = "requested", "Requested"
    ACCEPTED = "accepted", "Accepted"
    CANCELED = "canceled", "Canceled"


class ProposalStates(models.TextChoices):
    DRAFT = "draft", "Draft"
    SUBMITTED = "submitted", "Submitted"
    IN_REVIEW = "in_review", "In review"
    ACCEPTED = "accepted", "Accepted"
    REJECTED = "rejected", "Rejected"
    CANCELED = "canceled", "Canceled"


class ReviewStrategy(models.TextChoices):
    AFTER_ROUND = "after_round", "After round is closed"
    AFTER_PROPOSAL = "after_proposal", "After proposal submission"


class AllocationStrategy(models.TextChoices):
    BY_CALL_MANAGER = "by_call_manager", "By call manager"
    AUTOMATIC = "automatic", "Automatic based on review scoring"


class AllocationTime(models.TextChoices):
    ON_DECISION = "on_decision", "On decision"
    FIXED_DATE = "fixed_date", "Fixed date"


class ReviewState(models.TextChoices):
    CREATED = "created", "Created"
    IN_REVIEW = "in_review", "In review"
    SUBMITTED = "submitted", "Submitted"
    REJECTED = "rejected", "Rejected"
