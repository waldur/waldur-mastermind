import django.dispatch

# TODO: Make all the serializers emit this signal
# providing_args=['fields']
pre_serializer_fields = django.dispatch.Signal()

# This signal allows to implement deletion validation in dependent
# application without introducing circular dependency
# providing_args=['instance', 'user']
pre_delete_validate = django.dispatch.Signal()
