from django.dispatch import Signal

tenant_pull_succeeded = Signal(providing_args=['instance'])
