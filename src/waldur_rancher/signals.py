from django.dispatch import Signal

rancher_user_created = Signal(providing_args=['instance', 'password'])
