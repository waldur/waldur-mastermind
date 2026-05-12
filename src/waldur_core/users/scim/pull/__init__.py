"""On-demand outbound SCIM pull.

Pulls SCIM User resources from a remote SCIM 2.0 directory (configured via
``SCIM_PULL_API_URL`` + ``SCIM_PULL_API_KEY``) and merges them into Waldur via
the same source-aware attribute-merge helper used for inbound SCIM, OIDC, and
the Identity Bridge.

Pull is triggered explicitly — there is no periodic Celery beat task for it.
Use the ``scim_pull_user`` management command or the staff-only API action.
"""
