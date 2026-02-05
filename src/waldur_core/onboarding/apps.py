from django.apps import AppConfig
from django.db.models import signals


class OnboardingConfig(AppConfig):
    name = "waldur_core.onboarding"
    verbose_name = "Company Onboarding Validation"

    def ready(self):
        from waldur_core.onboarding import handlers
        from waldur_core.onboarding.models import OnboardingVerification

        signals.pre_delete.connect(
            handlers.log_verification_deleted,
            sender=OnboardingVerification,
            dispatch_uid="waldur_core.onboarding.handlers.log_verification_deleted",
        )
