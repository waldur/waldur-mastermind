from django.dispatch import Signal

node_states_have_been_updated = Signal(providing_args=['instance'])
rancher_user_created = Signal(providing_args=['instance', 'password'])
