from waldur_core.structure.exceptions import SerializableBackendError


class AnsibleBackendError(SerializableBackendError):
    pass


class LockedForProcessingError(Exception):
    pass
