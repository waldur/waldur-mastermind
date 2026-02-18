class KeycloakMembershipState:
    PENDING = "pending"
    ACTIVE = "active"

    CHOICES = (
        (PENDING, "pending"),
        (ACTIVE, "active"),
    )
