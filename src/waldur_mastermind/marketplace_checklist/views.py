from django.utils.translation import ugettext_lazy as _
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.mixins import ListModelMixin, CreateModelMixin
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from waldur_core.structure.models import Project
from waldur_core.structure.permissions import is_administrator

from . import models, serializers


class ChecklistView(ListModelMixin, GenericViewSet):
    queryset = models.Checklist.objects.all()
    serializer_class = serializers.ChecklistSerializer


class QuestionsView(ListModelMixin, GenericViewSet):
    serializer_class = serializers.QuestionSerializer

    def get_queryset(self):
        return models.Question.objects.filter(checklist__uuid=self.kwargs['checklist_uuid'])


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
                defaults={
                    'user': request.user,
                    'value': answer['value'],
                }
            )

        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)
