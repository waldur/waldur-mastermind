"""
Enums for onboarding app.
"""


class VerificationStatus:
    """Status choices for onboarding verification."""

    PENDING = "pending"
    VERIFIED = "verified"
    FAILED = "failed"
    ESCALATED = "escalated"
    EXPIRED = "expired"

    CHOICES = (
        (PENDING, "Pending"),
        (VERIFIED, "Verified"),
        (FAILED, "Failed"),
        (ESCALATED, "Escalated for manual validation"),
        (EXPIRED, "Expired"),
    )

    VALUES = [val for (val, _) in CHOICES]


class ReviewDecision:
    """Review decision choices for justifications."""

    APPROVED = "approved"
    REJECTED = "rejected"
    PENDING = "pending"

    CHOICES = (
        (APPROVED, "Approved"),
        (REJECTED, "Rejected"),
        (PENDING, "Pending Review"),
    )

    VALUES = [val for (val, _) in CHOICES]


class ValidationMethod:
    """Automatic validation method choices for onboarding verification."""

    ARIREGISTER = "ariregister"

    CHOICES = ((ARIREGISTER, "Estonian Business Register (ariregister)"),)

    VALUES = [val for (val, _) in CHOICES]
