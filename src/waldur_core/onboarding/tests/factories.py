import factory
from django.urls import reverse
from django.utils import timezone

from waldur_core.core.tests.types import BaseMetaFactory
from waldur_core.onboarding import models
from waldur_core.onboarding.enums import ReviewDecision, VerificationStatus
from waldur_core.structure.tests import factories as structure_factories


class OnboardingVerificationFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.OnboardingVerification],
):
    class Meta:
        model = models.OnboardingVerification

    user = factory.SubFactory(structure_factories.UserFactory)
    country = "EE"
    legal_person_identifier = factory.Sequence(lambda n: f"7000031{n}")
    legal_name = factory.Sequence(lambda n: f"Test Company {n}")
    status = VerificationStatus.PENDING
    validation_method = ""
    verified_user_roles = factory.List([])
    verified_company_data = factory.Dict({})
    raw_response = factory.Dict({})
    error_traceback = ""
    error_message = ""
    expires_at = factory.LazyFunction(
        lambda: timezone.now() + timezone.timedelta(hours=24)
    )

    @classmethod
    def get_url(cls, verification=None, action=None):
        verification = verification or OnboardingVerificationFactory()
        url = "http://testserver" + reverse(
            "onboarding-verification-detail", kwargs={"uuid": verification.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("onboarding-verification-list")
        return url if action is None else url + action + "/"


class OnboardingJustificationFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.OnboardingJustification],
):
    class Meta:
        model = models.OnboardingJustification

    verification = factory.SubFactory(OnboardingVerificationFactory)
    user = factory.SelfAttribute("verification.user")
    user_justification = "Please validate my company."
    validation_decision = ReviewDecision.PENDING
    staff_notes = "Notes from staff."

    @classmethod
    def get_url(cls, justification=None, action=None):
        justification = justification or OnboardingJustificationFactory()
        url = "http://testserver" + reverse(
            "onboarding-justification-detail", kwargs={"uuid": justification.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("onboarding-justification-list")
        return url if action is None else url + action + "/"
