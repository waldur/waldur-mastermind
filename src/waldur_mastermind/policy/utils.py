import logging

from django.utils import timezone

logger = logging.getLogger(__name__)


def evaluate_policies(policies):
    for policy in policies:
        if policy.is_triggered():
            if policy.has_fired:
                continue
            else:
                policy.has_fired = True
                policy.fired_datetime = timezone.now()
                policy.save()
                logger.info(
                    "A policy %s has fired.",
                    policy.uuid.hex,
                )

                for action in policy.get_immediate_actions():
                    action.method(policy)
                    logger.info(
                        "%s action of policy %s has been triggered.",
                        action.method.__name__,
                        policy.uuid.hex,
                    )
        else:
            if not policy.has_fired:
                continue
            else:
                policy.has_fired = False
                policy.fired_datetime = timezone.now()
                policy.save()
                logger.info(
                    "A policy %s has not fired.",
                    policy.uuid.hex,
                )

                logger.info("Resetting immediate actions")
                for action in policy.get_immediate_actions():
                    reset_method = action.reset_method
                    if reset_method:
                        logger.info(
                            "Running immediate action reset method %s.",
                            reset_method.__name__,
                        )
                        reset_method(policy)

                logger.info("Resetting threshold actions")
                for action in policy.get_threshold_actions():
                    reset_method = action.reset_method
                    if reset_method:
                        logger.info(
                            "Running threshold action reset method %s.",
                            reset_method.__name__,
                        )
                        reset_method(policy)
