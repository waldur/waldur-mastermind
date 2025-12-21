"""
Enums for onboarding app.
"""

from django.db import models


class VerificationStatus(models.TextChoices):
    """Status choices for onboarding verification."""

    PENDING = "pending", "Pending"
    VERIFIED = "verified", "Verified"
    FAILED = "failed", "Failed"
    ESCALATED = "escalated", "Escalated for manual validation"
    EXPIRED = "expired", "Expired"


class ReviewDecision(models.TextChoices):
    """Review decision choices for justifications."""

    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"
    PENDING = "pending", "Pending Review"


class ValidationMethod(models.TextChoices):
    """Automatic validation method choices for onboarding verification."""

    ARIREGISTER = "ariregister", "Estonian Business Register (ariregister)"
    WIRTSCHAFTSCOMPASS = (
        "wirtschaftscompass",
        "Austrian Business Register (WirtschaftsCompass)",
    )
    BOLAGSVERKET = "bolagsverket", "Swedish Business Register (Bolagsverket)"
    BRREG = "breg", "Norwegian Business Register (Brreg)"
