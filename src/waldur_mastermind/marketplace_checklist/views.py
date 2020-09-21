from django.db.models import F, Q
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
            customer_users = customer.get_users()
            correct_count = models.Answer.objects.filter(
                user__in=customer_users,
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
                        100
                        * correct_count
                        / max(1, customer_users.count() * total_questions),
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
            users = project.get_users()
            qs = models.Answer.objects.filter(
                user__in=users, question__checklist=checklist
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
        customer = get_object_or_404(Customer, uuid=customer_uuid)
        is_owner(request, self, customer)

        checklist = get_object_or_404(models.Checklist, uuid=checklist_uuid)
        total_questions = max(1, checklist.questions.count())
        points = []
        for project in Project.objects.filter(customer=customer).order_by('name'):
            project_users = project.get_users()
            customer_users = customer.get_owners()
            correct_count = (
                models.Answer.objects.filter(
                    Q(user__in=project_users) | Q(user__in=customer_users)
                )
                .filter(
                    question__checklist=checklist, value=F('question__correct_answer'),
                )
                .count()
            )
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
            user=self.request.user,
        )


class AnswersSubmitView(CreateModelMixin, GenericViewSet):
    serializer_class = serializers.AnswerSubmitSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data, many=True)
        serializer.is_valid(raise_exception=True)
        checklist = get_object_or_404(
            models.Checklist, uuid=self.kwargs['checklist_uuid']
        )

        for answer in serializer.validated_data:
            question = get_object_or_404(
                models.Question, uuid=answer['question_uuid'], checklist=checklist
            )
            models.Answer.objects.update_or_create(
                question=question,
                user=request.user,
                defaults={'value': answer['value']},
            )

        headers = self.get_success_headers(serializer.data)
        return Response(
            serializer.data, status=status.HTTP_201_CREATED, headers=headers
        )
