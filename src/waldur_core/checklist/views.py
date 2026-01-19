from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema
from rest_framework import permissions as rf_permissions
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from waldur_core.core import permissions as core_permissions
from waldur_core.core import views as core_views
from waldur_core.structure import filters as structure_filters

# Structure models imported dynamically to avoid circular dependencies
from . import filters, models, serializers


def get_score(num, den):
    return round(100 * num / max(1, den), 2)


class ChecklistAdminView(core_views.ActionsViewSet):
    queryset = models.Checklist.objects.all().order_by("-created")
    serializer_class = serializers.ChecklistSerializer
    filter_backends = [
        DjangoFilterBackend,
        structure_filters.GenericRoleFilter,
    ]
    lookup_field = "uuid"
    permission_classes = [rf_permissions.IsAuthenticated, core_permissions.IsStaff]
    filterset_class = filters.ChecklistFilter

    @extend_schema(
        description="Return checklist questions.",
        request=None,
        responses=serializers.QuestionAdminSerializer(many=True),
        operation_id="checklists_admin_checklist_questions",
    )
    @action(detail=True, methods=["get"])
    def questions(self, request, uuid=None):
        checklist = self.get_object()
        questions = checklist.questions.all().order_by("order")

        page = self.paginate_queryset(questions)
        if page is not None:
            serializer = serializers.QuestionAdminSerializer(
                page, context={"request": request}, many=True
            )
            return self.get_paginated_response(serializer.data)

        serializer = serializers.QuestionAdminSerializer(
            questions, context={"request": request}, many=True
        )
        return Response(serializer.data, status=status.HTTP_200_OK)


class QuestionsAdminView(core_views.ActionsViewSet):
    queryset = models.Question.objects.all().order_by("-checklist__created", "order")
    serializer_class = serializers.QuestionAdminSerializer
    filter_backends = [
        DjangoFilterBackend,
        structure_filters.GenericRoleFilter,
    ]
    lookup_field = "uuid"
    permission_classes = [rf_permissions.IsAuthenticated, core_permissions.IsStaff]
    filterset_class = filters.QuestionFilter


class QuestionOptionAdminViewSet(core_views.ActionsViewSet):
    queryset = models.QuestionOption.objects.select_related("question").order_by(
        "question__checklist", "question"
    )
    serializer_class = serializers.QuestionOptionsAdminSerializer
    filter_backends = [
        DjangoFilterBackend,
        structure_filters.GenericRoleFilter,
    ]
    permission_classes = [rf_permissions.IsAuthenticated, core_permissions.IsStaff]
    lookup_field = "uuid"
    filterset_class = filters.QuestionOptionFilter


class QuestionDependencyViewSet(core_views.ActionsViewSet):
    serializer_class = serializers.QuestionDependencySerializer
    permission_classes = [rf_permissions.IsAuthenticated, core_permissions.IsStaff]
    lookup_field = "uuid"
    queryset = models.QuestionDependency.objects.all()
    filterset_class = filters.QuestionDependencyFilter
