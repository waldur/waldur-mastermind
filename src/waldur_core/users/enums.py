from typing import Literal


class InvitationState:
    PENDING_PROJECT = "project"
    REQUESTED = "requested"
    REJECTED = "rejected"
    PENDING = "pending"
    ACCEPTED = "accepted"
    CANCELED = "canceled"
    EXPIRED = "expired"

    CHOICES = (
        (PENDING_PROJECT, "Pending project"),
        (REQUESTED, "Requested"),
        (REJECTED, "Rejected"),
        (PENDING, "Pending"),
        (ACCEPTED, "Accepted"),
        (CANCELED, "Canceled"),
        (EXPIRED, "Expired"),
    )

    VALUES = [val for (val, _) in CHOICES]


InvitationStateType = Literal[
    "project",
    "requested",
    "rejected",
    "pending",
    "accepted",
    "canceled",
    "expired",
]
