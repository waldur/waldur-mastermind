from django.apps import AppConfig


class PolicyConfig(AppConfig):
    name = 'waldur_mastermind.policy'
    verbose_name = 'Policy'

    def ready(self):
        from django.db.models import signals

        from waldur_mastermind.policy import handlers

        from . import models

        signals.post_save.connect(
            handlers.project_estimated_cost_policy_handler,
            sender=models.ProjectEstimatedCostPolicy.trigger_class,
            dispatch_uid='project_estimated_cost_policy_handler',
        )

        for klass in models.ProjectEstimatedCostPolicy.observable_classes:
            signals.post_save.connect(
                handlers.project_estimated_cost_policy_handler_for_observable_class,
                sender=klass,
                dispatch_uid='project_estimated_cost_policy_handler_for_observable_class',
            )
