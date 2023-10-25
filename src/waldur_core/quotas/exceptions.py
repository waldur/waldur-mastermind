from rest_framework.exceptions import ValidationError


class QuotaError(Exception):
    pass


class QuotaValidationError(ValidationError):
    pass
