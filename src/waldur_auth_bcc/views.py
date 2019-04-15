from __future__ import unicode_literals

from django.conf import settings
from rest_framework import views
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from . import client, serializers


class UserDetailsViewSet(views.APIView):

    def get(self, request, *args, **kwargs):
        if not settings.WALDUR_AUTH_BCC['ENABLED']:
            raise ValidationError('This feature is disabled.')

        serializer = serializers.UserDetailRequestSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)

        civil_number = serializer.validated_data['civil_number']
        tax_number = serializer.validated_data['tax_number']

        try:
            user_details = client.get_user_details(civil_number, tax_number)
            return Response(user_details._asdict())
        except client.BCCException as e:
            return Response({'details': e.detail}, status=e.code)
