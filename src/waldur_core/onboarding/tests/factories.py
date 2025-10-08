import factory
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
    user_submitted_customer_metadata = factory.Dict({})
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


class OnboardingJustificationFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.OnboardingJustification],
):
    class Meta:
        model = models.OnboardingJustification

    verification = factory.SubFactory(OnboardingVerificationFactory)
    user = factory.SelfAttribute("verification.user")
    user_justification = factory.Faker("text", max_nb_chars=500)
    validation_decision = ReviewDecision.PENDING
    staff_notes = ""
