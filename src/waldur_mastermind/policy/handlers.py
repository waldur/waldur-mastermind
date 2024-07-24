import logging

from django.utils import timezone

logger = logging.getLogger(__name__)


def get_estimated_cost_policy_handler(klass):
    def handler(sender, instance, created=False, **kwargs):
        if not isinstance(instance.scope, klass.get_scope_class()):
            return

        scope = instance.scope
        policies = klass.objects.filter(scope=scope)

        for policy in policies:
            if not policy.has_fired and policy.is_triggered():
                policy.has_fired = True
                policy.fired_datetime = timezone.now()
                policy.save()

                for action in policy.get_one_time_actions():
                    action(policy)
                    logger.info(
                        "%s action has been triggered for %s. Policy UUID: %s",
                        action.__name__,
                        policy.scope.name,
                        policy.uuid.hex,
                    )

            elif policy.has_fired and not policy.is_triggered():
                policy.has_fired = False
                policy.save()

    return handler


def get_estimated_cost_policy_handler_for_observable_class(klass, observable_class):
    def handler(sender, instance, created=False, **kwargs):
        if not isinstance(instance, observable_class):
            return

        observable_object = instance
        policies = klass.objects.filter(
            scope=klass.get_scope_from_observable_object(observable_object)
        )

        for policy in policies:
            if policy.is_triggered():
                for action in policy.get_not_one_time_actions():
                    action(policy, created)
                    logger.info(
                        "%s action has been triggered for %s. Policy UUID: %s",
                        action.__name__,
                        policy.scope.name,
                        policy.uuid.hex,
                    )

    return handler
