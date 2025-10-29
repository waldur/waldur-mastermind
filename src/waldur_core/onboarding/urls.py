from django.urls import path

from . import views


def register_in(router):
    router.register(
        r"onboarding-verifications",
        views.OnboardingVerificationViewSet,
        basename="onboarding-verification",
    )
    router.register(
        r"onboarding-justifications",
        views.OnboardingJustificationViewSet,
        basename="onboarding-justification",
    )
    router.register(
        r"onboarding-country-configs",
        views.OnboardingCountryChecklistConfigurationViewSet,
        basename="onboarding-country-config",
    )
    router.register(
        r"onboarding-question-metadata",
        views.OnboardingQuestionMetadataViewSet,
        basename="onboarding-question-metadata",
    )


urlpatterns = [
    path(
        "onboarding/supported-countries/",
        views.SupportedCountriesView.as_view(),
        name="onboarding-supported-countries",
    ),
]
