class CallStates:
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"

    CHOICES = (
        (DRAFT, "Draft"),
        (ACTIVE, "Active"),
        (ARCHIVED, "Archived"),
    )


class RoundStatuses:
    SCHEDULED = "scheduled"
    OPEN = "open"
    ENDED = "ended"

    CHOICES = (
        (SCHEDULED, "Round is scheduled"),
        (OPEN, "Round is open"),
        (ENDED, "Round is ended"),
    )

    VALUES = [val for (val, _) in CHOICES]


class RequestedOfferingStates:
    REQUESTED = "requested"
    ACCEPTED = "accepted"
    CANCELED = "canceled"

    CHOICES = (
        (REQUESTED, "Requested"),
        (ACCEPTED, "Accepted"),
        (CANCELED, "Canceled"),
    )


class ProposalStates:
    DRAFT = "draft"
    SUBMITTED = "submitted"
    IN_REVIEW = "in_review"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CANCELED = "canceled"

    CHOICES = (
        (DRAFT, "Draft"),
        (SUBMITTED, "Submitted"),
        (IN_REVIEW, "In review"),
        (ACCEPTED, "Accepted"),
        (REJECTED, "Rejected"),
        (CANCELED, "Canceled"),
    )
