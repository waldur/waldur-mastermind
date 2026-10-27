from django.db import transaction
from django.db.models import Prefetch
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema
from rest_framework import decorators, exceptions, status
from rest_framework.response import Response

from waldur_core.checklist import models as checklist_models
from waldur_core.checklist import serializers as checklist_serializers
from waldur_core.checklist.mixins import UserChecklistMixin
from waldur_core.core import exceptions as core_exceptions
from waldur_core.core import validators as core_validators
from waldur_core.core.views import ActionsViewSet
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.structure import permissions as structure_permissions

from . import filters, models, serializers, utils


def is_request_owner(request, view, obj=None):
    if obj is not None and obj.user != request.user:
        raise exceptions.PermissionDenied()


def survey_is_open(feedback_request):
    if not feedback_request.survey.is_open:
        raise core_exceptions.IncorrectStateException("The survey is closed.")


def lock_pending(feedback_request):
    """Lock the request row and re-check it is still pending."""
    locked = models.FeedbackRequest.objects.select_for_update().get(
        pk=feedback_request.pk
    )
    if locked.state != models.FeedbackRequest.States.PENDING:
        raise core_exceptions.IncorrectStateException(
            "The request is no longer pending."
        )
    return locked


class FeedbackSurveyViewSet(ActionsViewSet):
    queryset = models.FeedbackSurvey.objects.select_related("checklist")
    serializer_class = serializers.FeedbackSurveySerializer
    filter_backends = [DjangoFilterBackend]
    filterset_class = filters.FeedbackSurveyFilterSet
    lookup_field = "uuid"
    safe_methods_permissions = [structure_permissions.is_staff_or_support]
    unsafe_methods_permissions = [structure_permissions.is_staff]

    @extend_schema(
        description="Return the survey to show the current user now, if any, "
        "and count it as a prompt.",
        request=None,
        responses={
            status.HTTP_200_OK: serializers.FeedbackPromptSerializer,
            status.HTTP_204_NO_CONTENT: None,
        },
    )
    @decorators.action(detail=False, methods=["post"])
    def prompt(self, request):
        feedback_request = utils.get_prompt(request.user)
        if not feedback_request:
            return Response(status=status.HTTP_204_NO_CONTENT)
        return Response(serializers.FeedbackPromptSerializer(feedback_request).data)

    prompt_permissions = []

    @extend_schema(responses=serializers.FeedbackSurveySummarySerializer)
    @decorators.action(detail=True, methods=["get"])
    def summary(self, request, uuid=None):
        survey = self.get_object()
        return Response(
            serializers.FeedbackSurveySummarySerializer(
                utils.get_survey_summary(survey)
            ).data
        )


class FeedbackRequestViewSet(UserChecklistMixin, ActionsViewSet):
    queryset = models.FeedbackRequest.objects.all()
    serializer_class = serializers.FeedbackRequestSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_class = filters.FeedbackRequestFilterSet
    lookup_field = "uuid"
    disabled_actions = [
        "create",
        "update",
        "partial_update",
        "destroy",
        "checklist_template",
    ]

    checklist_permissions = [is_request_owner]
    completion_status_permissions = [is_request_owner]
    submit_answers_permissions = [is_request_owner]
    later_permissions = [is_request_owner]
    dismiss_permissions = [is_request_owner]

    pending_validators = [
        core_validators.StateValidator(models.FeedbackRequest.States.PENDING),
        survey_is_open,
    ]
    submit_answers_validators = later_validators = dismiss_validators = (
        pending_validators
    )

    def get_object(self):
        # The validators, the checklist mixin and the actions each ask for it.
        if not hasattr(self, "_object"):
            self._object = super().get_object()
        return self._object

    def get_queryset(self):
        queryset = (
            super()
            .get_queryset()
            .select_related("survey", "user")
            .prefetch_related(
                Prefetch(
                    "checklist_completions",
                    queryset=checklist_models.ChecklistCompletion.objects.prefetch_related(
                        "answers__question__question_options"
                    ),
                )
            )
        )
        user = self.request.user
        if user.is_staff or user.is_support:
            return queryset
        return queryset.filter(user=user)

    @extend_schema(
        description="Submit answers. A complete submission marks the request answered.",
        request=checklist_serializers.AnswerSubmitSerializer(many=True),
        responses={200: checklist_serializers.AnswerSubmitResponseSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    @transaction.atomic
    def submit_answers(self, request, uuid=None):
        lock_pending(self.get_object())
        response = super().submit_answers(request, uuid)
        feedback_request = self.get_object()
        feedback_request.refresh_from_db()
        completion = feedback_request.checklist_completion
        if completion and utils.is_answered(completion):
            feedback_request.state = models.FeedbackRequest.States.ANSWERED
            feedback_request.answered_at = timezone.now()
            feedback_request.save(update_fields=["state", "answered_at", "modified"])
            event_logger.emit(
                "User {user_username} has answered feedback survey {survey_name}.",
                event_type=EventType.FEEDBACK_SURVEY_ANSWERED,
                event_context={
                    "user": request.user,
                    "survey_name": feedback_request.survey.name,
                    "survey_uuid": feedback_request.survey.uuid.hex,
                },
                scopes=[request.user],
            )
        return response

    @extend_schema(
        description="Postpone the prompt. Answers given so far are kept as a draft, "
        "leaving out any that are not valid; the request stays pending.",
        request=checklist_serializers.AnswerSubmitSerializer(many=True),
        responses={status.HTTP_200_OK: None},
    )
    @decorators.action(detail=True, methods=["post"])
    @transaction.atomic
    def later(self, request, uuid=None):
        feedback_request = lock_pending(self.get_object())
        completion = feedback_request.checklist_completion
        if completion and isinstance(request.data, list):
            # Each answer on its own, so one invalid answer doesn't lose the rest.
            draft = []
            for item in request.data:
                serializer = checklist_serializers.AnswerSubmitSerializer(
                    data=item, context={"completion": completion, "request": request}
                )
                if serializer.is_valid():
                    draft.append(serializer.validated_data)
            self.save_answers(completion, draft, request.user)
        feedback_request.snoozed_until = timezone.now() + timezone.timedelta(
            days=feedback_request.survey.snooze_days
        )
        feedback_request.save(update_fields=["snoozed_until", "modified"])
        return Response(status=status.HTTP_200_OK)

    @extend_schema(request=None, responses={status.HTTP_200_OK: None})
    @decorators.action(detail=True, methods=["post"])
    @transaction.atomic
    def dismiss(self, request, uuid=None):
        feedback_request = lock_pending(self.get_object())
        feedback_request.state = models.FeedbackRequest.States.DISMISSED
        feedback_request.save(update_fields=["state", "modified"])
        return Response(status=status.HTTP_200_OK)
