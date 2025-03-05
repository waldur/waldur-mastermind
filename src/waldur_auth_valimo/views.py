from django.http import Http404
from drf_spectacular.utils import (
    OpenApiExample,
    extend_schema,
)
from rest_framework import decorators, mixins, response, viewsets

from waldur_core.core import mixins as core_mixins
from waldur_core.core.views import validate_authentication_method

from . import executors, models, serializers

validate_valimo = validate_authentication_method("VALIMO")


class AuthResultViewSet(
    core_mixins.CreateExecutorMixin, mixins.CreateModelMixin, viewsets.GenericViewSet
):
    queryset = models.AuthResult.objects.all().order_by("user")
    serializer_class = serializers.AuthResultSerializer
    permission_classes = ()
    lookup_field = "uuid"
    create_executor = executors.AuthExecutor

    @extend_schema(
        request=serializers.AuthResultSerializer,
        responses={
            201: serializers.AuthResultSerializer,
        },
        examples=[
            OpenApiExample(
                "Valid request",
                summary="Start PKI login process",
                description="Example of a valid request to start PKI login process with user's phone.",
                value={
                    "phone": "1234567890",
                },
                request_only=True,
            ),
        ],
    )
    @validate_valimo
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    @extend_schema(
        description="""
        To get PKI login status and details - issue post request against /api/auth-valimo/result/
        with uuid in parameters.

        Possible states:
         - Scheduled - login process is scheduled
         - Processing - login is in progress
         - OK - login was successful. Response will contain token.
         - Canceled - login was canceled by user or timed out. Field details will contain additional info.
         - Erred - unexpected exception happened during login process.
        """,
        request=serializers.AuthResultUUIDSerializer,
        responses={
            200: serializers.AuthResultSerializer,
        },
        examples=[
            OpenApiExample(
                "Successful login",
                summary="Successful login",
                description="Example of response for successful login.",
                value={
                    "uuid": "e42473f39c844333a80107e139a4dd06",
                    "token": None,
                    "message": "1234",
                    "state": "OK",
                    "error_message": "",
                    "details": "User authenticated.",
                },
                response_only=True,
            ),
            OpenApiExample(
                "Canceled login",
                summary="Canceled login",
                description="Example of response when login was canceled by user.",
                value={
                    "uuid": "e42473f39c844333a80107e139a4dd06",
                    "token": None,
                    "message": "1234",
                    "state": "Canceled",
                    "error_message": "",
                    "details": "User cancel.",
                },
                response_only=True,
            ),
        ],
    )
    @validate_valimo
    @decorators.action(detail=False, methods=["POST"])
    def result(self, request, *args, **kwargs):
        try:
            auth_result = models.AuthResult.objects.get(uuid=request.data.get("uuid"))
        except models.AuthResult.DoesNotExist:
            raise Http404
        serializer = self.get_serializer(auth_result)
        return response.Response(serializer.data)
