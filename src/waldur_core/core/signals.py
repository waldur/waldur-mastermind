import django.dispatch

# TODO: Make all the serializers emit this signal
pre_serializer_fields = django.dispatch.Signal(providing_args=['fields'])

# This signal allows to implement deletion validation in dependent
# application without introducing circular dependency
pre_delete_validate = django.dispatch.Signal(providing_args=['instance', 'user'])
