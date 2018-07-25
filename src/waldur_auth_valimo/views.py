from __future__ import unicode_literals

from django.http import Http404
from rest_framework import viewsets, mixins, response, decorators

from waldur_core.core import mixins as core_mixins
from waldur_core.core.views import validate_authentication_method

from . import models, serializers, executors


validate_valimo = validate_authentication_method('VALIMO')


class AuthResultViewSet(core_mixins.CreateExecutorMixin,
                        mixins.CreateModelMixin,
                        viewsets.GenericViewSet):
    queryset = models.AuthResult.objects.all()
    serializer_class = serializers.AuthResultSerializer
    permission_classes = ()
    lookup_field = 'uuid'
    create_executor = executors.AuthExecutor

    @validate_valimo
    def create(self, request, *args, **kwargs):
        """
        To start PKI login process - issue post request with users phone.
        Example of a valid request:

        .. code-block:: http

            POST /api/auth-valimo/ HTTP/1.1
            Content-Type: application/json
            Accept: application/json
            Host: example.com

            {
                "phone": "1234567890",
            }
        """
        return super(AuthResultViewSet, self).create(request, *args, **kwargs)

    @validate_valimo
    @decorators.list_route(methods=['POST'])
    def result(self, request, *args, **kwargs):
        """
        To get PKI login status and details - issue post request against /api/auth-valimo/result/
        with uuid in parameters.

        Possible states:
         - Scheduled - login process is scheduled
         - Processing - login is in progress
         - OK - login was successful. Response will contain token.
         - Canceled - login was canceled by user or timed out. Field details will contain additional info.
         - Erred - unexpected exception happened during login process.
        Example of response:

        .. code-block:: javascript

            {
                "uuid": "e42473f39c844333a80107e139a4dd06",
                "token": null,
                "message": "1234",
                "state": "Canceled",
                "error_message": "",
                "details": "User cancel."
            }
        """
        try:
            auth_result = models.AuthResult.objects.get(uuid=request.data.get('uuid'))
        except models.AuthResult.DoesNotExist:
            raise Http404
        serializer = self.get_serializer(auth_result)
        return response.Response(serializer.data)
