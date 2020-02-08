from django.dispatch import Signal

node_states_have_been_updated = Signal(providing_args=['instance'])
rancher_user_has_been_synchronized = Signal(providing_args=['instance', 'password'])
