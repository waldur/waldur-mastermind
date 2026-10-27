import factory
from django.urls import reverse
from django.utils import timezone

from waldur_core.checklist import enums as checklist_enums
from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.core.tests.types import BaseMetaFactory
from waldur_mastermind.feedback import models


class FeedbackSurveyFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.FeedbackSurvey]
):
    class Meta:
        model = models.FeedbackSurvey

    name = factory.Sequence(lambda n: "survey-%s" % n)
    checklist = factory.SubFactory(
        checklist_factories.ChecklistFactory,
        checklist_type=checklist_enums.ChecklistTypes.FEEDBACK,
    )
    starts_at = factory.LazyFunction(
        lambda: timezone.now() - timezone.timedelta(days=1)
    )
    ends_at = factory.LazyFunction(lambda: timezone.now() + timezone.timedelta(days=7))
    audience = {"all_users": True}

    @classmethod
    def get_url(cls, survey=None, action=None):
        survey = survey or FeedbackSurveyFactory()
        url = "http://testserver" + reverse(
            "feedbacksurvey-detail", kwargs={"uuid": survey.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("feedbacksurvey-list")
        return url if action is None else url + action + "/"


def get_request_url(request=None, action=None):
    if request is None:
        url = "http://testserver" + reverse("feedbackrequest-list")
        return url if action is None else url + action + "/"
    url = "http://testserver" + reverse(
        "feedbackrequest-detail", kwargs={"uuid": request.uuid.hex}
    )
    return url if action is None else url + action + "/"
