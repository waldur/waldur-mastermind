class CallStates:
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"

    CHOICES = (
        (DRAFT, "Draft"),
        (ACTIVE, "Active"),
        (ARCHIVED, "Archived"),
    )


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
