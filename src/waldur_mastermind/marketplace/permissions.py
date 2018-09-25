from django.conf import settings as django_settings
from rest_framework import exceptions

from waldur_core.structure import permissions as structure_permissions


def can_register_service_provider(request, customer):
    if request.user.is_staff:
        return
    if not django_settings.WALDUR_MARKETPLACE['OWNER_CAN_REGISTER_SERVICE_PROVIDER']:
        raise exceptions.PermissionDenied()
    structure_permissions.is_owner(request, None, customer)
