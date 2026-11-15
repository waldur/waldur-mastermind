"""Documentation search over REST, for portals without the AI assistant.

The same search the assistant's ``search_docs`` tool runs, filtered the same
way by role and site configuration. It needs no LLM, so it does not depend on
AI_ASSISTANT_ENABLED; it needs only the corpus shipped in the image, and
DOCS_SEARCH_ENABLED, which staff turn off to keep the bundled documentation
out of the portal.
"""

from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers as rf_serializers
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from waldur_core.core import access as core_access
from waldur_core.core import permissions as core_permissions
from waldur_core.core.views import ConstanceCheckExtensionMixin
from waldur_mastermind.chat.docs import corpus, search, serializers


class DocsSearchView(ConstanceCheckExtensionMixin, APIView):
    # 424 while DOCS_SEARCH_ENABLED is off.
    extension_name = "DOCS_SEARCH"

    # A token with fewer scopes than its user's role would otherwise see the
    # staff and support sections the user's role admits.
    permission_classes = [IsAuthenticated, core_permissions.TokenCarriesUserRole]
    access_declarations = {
        "get": [
            core_access.custom(
                "Any signed-in user; the sections returned depend on the "
                "caller's role and on what is switched on here."
            )
        ]
    }

    @extend_schema(
        parameters=[serializers.DocsSearchRequestSerializer],
        responses={
            status.HTTP_200_OK: serializers.DocsSearchResponseSerializer,
            status.HTTP_424_FAILED_DEPENDENCY: inline_serializer(
                "DocsSearchDisabled",
                fields={"detail": rf_serializers.CharField()},
            ),
            status.HTTP_503_SERVICE_UNAVAILABLE: inline_serializer(
                "DocsSearchUnavailable",
                fields={"detail": rf_serializers.CharField()},
            ),
        },
    )
    def get(self, request):
        """Search the documentation of this release.

        End users get only the sections for their role whose features are
        switched on here. Staff and support also get sections for switched-off
        features, marked ``enabled_here: false``. Answers 424 while
        DOCS_SEARCH_ENABLED is off and 503 when no documentation corpus
        is loaded.
        """
        params = serializers.DocsSearchRequestSerializer(data=request.query_params)
        params.is_valid(raise_exception=True)
        if not corpus.is_available():
            return Response(
                {"detail": "This deployment has no documentation to search."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        query = params.validated_data["query"]
        hits = search.search(request.user, query, params.validated_data["limit"])
        response = serializers.DocsSearchResponseSerializer(
            {"query": query, "sections": [hit.as_dict() for hit in hits]}
        )
        return Response(response.data)
