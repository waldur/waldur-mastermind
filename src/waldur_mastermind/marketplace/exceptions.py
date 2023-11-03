from rest_framework import exceptions


class ResourceTerminateException(Exception):
    pass


class PolicyException(exceptions.ValidationError):
    pass
