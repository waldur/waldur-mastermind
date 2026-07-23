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
    WIRTSCHAFTSCOMPASS = "wirtschaftscompass"
    BOLAGSVERKET = "bolagsverket"
    BRREG = "breg"
    DNB_SE = "dnb_se"
    DNB_NO = "dnb_no"
    DNB_DK = "dnb_dk"
    DNB_FI = "dnb_fi"

    CHOICES = (
        (ARIREGISTER, "Estonian Business Register (ariregister)"),
        (WIRTSCHAFTSCOMPASS, "Austrian Business Register (WirtschaftsCompass)"),
        (BOLAGSVERKET, "Swedish Business Register (Bolagsverket)"),
        (BRREG, "Norwegian Business Register (Brreg)"),
        (DNB_SE, "Dun & Bradstreet Sweden"),
        (DNB_NO, "Dun & Bradstreet Norway"),
        (DNB_DK, "Dun & Bradstreet Denmark"),
        (DNB_FI, "Dun & Bradstreet Finland"),
    )

    VALUES = [val for (val, _) in CHOICES]
