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


urlpatterns = [
    path(
        "onboarding/supported-countries/",
        views.SupportedCountriesView.as_view(),
        name="onboarding-supported-countries",
    ),
]
