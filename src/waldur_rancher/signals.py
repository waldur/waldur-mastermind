from django.dispatch import Signal

# providing_args=['instance', 'password']
rancher_user_created = Signal()
