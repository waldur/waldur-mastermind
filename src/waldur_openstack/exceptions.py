from waldur_core.structure.exceptions import SerializableBackendError


class OpenStackBackendError(SerializableBackendError):
    pass


class OpenStackSessionExpired(OpenStackBackendError):
    pass


class OpenStackAuthorizationFailed(OpenStackBackendError):
    pass


class OpenStackTenantNotFound(OpenStackBackendError):
    pass


class OpenStackRBACPolicyDuplicate(OpenStackBackendError):
    """Neutron already holds a policy for this (network, target, action).

    Kept distinct from the generic backend error so the API layer can answer
    409 rather than 500: a duplicate is a request the caller can correct, not
    an internal failure.
    """
