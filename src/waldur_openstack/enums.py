class Ipv6Modes:
    """Neutron's IPv6 address modes, for a subnet's ``ipv6_ra_mode`` and
    ``ipv6_address_mode``.

    Kept out of models.py so the OpenAPI settings can name the choice set
    without importing models.
    """

    SLAAC = "slaac"
    DHCPV6_STATEFUL = "dhcpv6-stateful"
    DHCPV6_STATELESS = "dhcpv6-stateless"
    # The instance builds its address from the prefix itself, which Neutron
    # only allows on a /64.
    FROM_PREFIX = (SLAAC, DHCPV6_STATELESS)
    CHOICES = (
        (SLAAC, "SLAAC"),
        (DHCPV6_STATEFUL, "DHCPv6 stateful"),
        (DHCPV6_STATELESS, "DHCPv6 stateless"),
    )
