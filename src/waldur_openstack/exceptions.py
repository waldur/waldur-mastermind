from waldur_core.structure.exceptions import SerializableBackendError


class OpenStackBackendError(SerializableBackendError):
    pass


class OpenStackSessionExpired(OpenStackBackendError):
    pass


class OpenStackAuthorizationFailed(OpenStackBackendError):
    pass


class OpenStackTenantNotFound(OpenStackBackendError):
    pass
