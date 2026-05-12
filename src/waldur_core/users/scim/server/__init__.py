"""Inbound SCIM 2.0 Service Provider for Waldur.

Mounted at /scim/v2/. Allows external identity providers (Okta, Microsoft Entra ID,
Keycloak, JumpCloud, ...) to provision users and groups into Waldur using the
standard RFC 7643/7644 protocol.

Disabled by default; gated by Constance setting SCIM_INBOUND_ENABLED.
Authenticated via bearer token tied to a staff service-account User.
"""
