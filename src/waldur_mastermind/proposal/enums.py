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
    TEAM_VERIFICATION = "team_verification"
    SUBMITTED = "submitted"
    IN_REVIEW = "in_review"
    IN_REVISION = "in_revision"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CANCELED = "canceled"

    CHOICES = (
        (DRAFT, "Draft"),
        (TEAM_VERIFICATION, "Team verification"),
        (SUBMITTED, "Submitted"),
        (IN_REVIEW, "In review"),
        (IN_REVISION, "In revision"),
        (ACCEPTED, "Accepted"),
        (REJECTED, "Rejected"),
        (CANCELED, "Canceled"),
    )
