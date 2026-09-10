import django.dispatch

# TODO: Make all the serializers emit this signal
# providing_args=['fields']
pre_serializer_fields = django.dispatch.Signal()

# This signal allows to implement deletion validation in dependent
# application without introducing circular dependency
# providing_args=['instance', 'user']
pre_delete_validate = django.dispatch.Signal()

# Sent once a user's attributes have been refreshed from an identity provider —
# after an OIDC login and after a SCIM pull. Distinct from ``post_save`` on User:
# it fires only when identity data actually arrived, which is the moment claims
# may have changed. Optional apps (auto-provisioning) hook role reconciliation
# here rather than being imported from the auth code, which must keep working
# when they are not installed.
# providing_args=['user', 'source', 'created']
user_identity_synced = django.dispatch.Signal()
