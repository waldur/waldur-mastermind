from django.db.models import F
from django.utils.translation import ugettext_lazy as _
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.generics import get_object_or_404
from rest_framework.mixins import CreateModelMixin, ListModelMixin, RetrieveModelMixin
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import GenericViewSet

from waldur_core.structure.models import Customer, Project
from waldur_core.structure.permissions import is_administrator, is_owner

from . import models, serializers


class CategoriesView(RetrieveModelMixin, ListModelMixin, GenericViewSet):
    queryset = models.Category.objects.all()
    serializer_class = serializers.CategorySerializer
    lookup_field = 'uuid'


class CategoryChecklistsView(ListModelMixin, GenericViewSet):
    serializer_class = serializers.ChecklistSerializer

    def get_queryset(self):
        return models.Checklist.objects.filter(
            category__uuid=self.kwargs['category_uuid']
        )


class ChecklistView(ListModelMixin, GenericViewSet):
    queryset = models.Checklist.objects.all()
    serializer_class = serializers.ChecklistSerializer


class QuestionsView(ListModelMixin, GenericViewSet):
    serializer_class = serializers.QuestionSerializer

    def get_queryset(self):
        return models.Question.objects.filter(
            checklist__uuid=self.kwargs['checklist_uuid']
        )


class StatsView(APIView):
    def get(self, request, checklist_uuid, format=None):
        if not request.user.is_staff and not request.user.is_support:
            raise PermissionDenied()

        checklist = get_object_or_404(models.Checklist, uuid=checklist_uuid)
        total_questions = checklist.questions.count()
        points = []
        for customer in Customer.objects.all():
            projects_count = customer.projects.count()
            correct_count = models.Answer.objects.filter(
                project__in=customer.projects.all(),
                question__checklist=checklist,
                value=F('question__correct_answer'),
            ).count()
            points.append(
                dict(
                    name=customer.name,
                    uuid=customer.uuid,
                    latitude=customer.latitude,
                    longitude=customer.longitude,
                    score=round(
                        100 * correct_count / max(1, projects_count * total_questions),
                        2,
                    ),
                )
            )
        return Response(points)


class ProjectStatsView(APIView):
    def get(self, request, project_uuid, format=None):
        try:
            project = Project.objects.get(uuid=project_uuid)
        except Project.DoesNotExist:
            raise ValidationError(_('Project does not exist.'))

        is_administrator(request, self, project)

        checklists = []
        for checklist in models.Checklist.objects.all():
            qs = models.Answer.objects.filter(
                project=project, question__checklist=checklist
            )
            total = checklist.questions.count()
            positive_count = qs.filter(value=F('question__correct_answer')).count()
            negative_count = (
                qs.exclude(value__isnull=True)
                .exclude(value=F('question__correct_answer'))
                .count()
            )
            unknown_count = total - positive_count - negative_count
            checklists.append(
                dict(
                    name=checklist.name,
                    uuid=checklist.uuid,
                    positive_count=positive_count,
                    negative_count=negative_count,
                    unknown_count=unknown_count,
                    score=round(100 * positive_count / total, 2)
                    if total > 0
                    else 100,  # consider empty lists as fully compliant
                )
            )
        return Response(checklists)


class CustomerStatsView(APIView):
    def get(self, request, customer_uuid, checklist_uuid, format=None):
        try:
            customer = Customer.objects.get(uuid=customer_uuid)
        except Customer.DoesNotExist:
            raise ValidationError(_('Customer does not exist.'))

        is_owner(request, self, customer)

        checklist = get_object_or_404(models.Checklist, uuid=checklist_uuid)
        total_questions = max(1, checklist.questions.count())
        points = []
        for project in Project.objects.filter(customer=customer).order_by('name'):
            correct_count = models.Answer.objects.filter(
                project=project,
                question__checklist=checklist,
                value=F('question__correct_answer'),
            ).count()
            points.append(
                dict(
                    name=project.name,
                    uuid=project.uuid.hex,
                    score=round(100 * correct_count / total_questions, 2,),
                )
            )
        return Response(points)


class AnswersListView(ListModelMixin, GenericViewSet):
    serializer_class = serializers.AnswerListSerializer

    def get_queryset(self):
        return models.Answer.objects.filter(
            question__checklist__uuid=self.kwargs['checklist_uuid'],
            project__uuid=self.kwargs['project_uuid'],
        )


class AnswersSubmitView(CreateModelMixin, GenericViewSet):
    serializer_class = serializers.AnswerSubmitSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data, many=True)
        serializer.is_valid(raise_exception=True)

        try:
            project = Project.objects.get(uuid=self.kwargs['project_uuid'])
        except Project.DoesNotExist:
            raise ValidationError(_('Project does not exist.'))

        is_administrator(request, self, project)

        try:
            checklist = models.Checklist.objects.get(uuid=self.kwargs['checklist_uuid'])
        except models.Checklist.DoesNotExist:
            raise ValidationError(_('Checklist does not exist.'))

        for answer in serializer.validated_data:
            try:
                question = checklist.questions.get(uuid=answer['question_uuid'])
            except models.Question.DoesNotExist:
                raise ValidationError(_('Question does not exist.'))

            models.Answer.objects.update_or_create(
                question=question,
                project=project,
                defaults={'user': request.user, 'value': answer['value'],},
            )

        headers = self.get_success_headers(serializer.data)
        return Response(
            serializer.data, status=status.HTTP_201_CREATED, headers=headers
        )
