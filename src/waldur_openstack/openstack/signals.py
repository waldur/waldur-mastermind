from django.dispatch import Signal

# providing_args=['instance']
tenant_pull_succeeded = Signal()
tenant_does_not_exist_in_backend = Signal()
