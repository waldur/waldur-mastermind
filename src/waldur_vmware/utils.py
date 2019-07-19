from django.conf import settings


def is_basic_mode():
    return settings.WALDUR_VMWARE.get('BASIC_MODE')
