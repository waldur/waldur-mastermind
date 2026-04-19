import logging

from django.utils import timezone

logger = logging.getLogger(__name__)


def evaluate_policies(policies):
    for policy in policies:
        if policy.is_triggered():
            # Atomic CAS: only fire if has_fired is still False in DB.
            # Prevents double-fire when concurrent workers evaluate the same policy.
            updated = (
                type(policy)
                .objects.filter(pk=policy.pk, has_fired=False)
                .update(has_fired=True, fired_datetime=timezone.now())
            )
            if updated == 0:
                continue  # Already fired by another worker

            policy.refresh_from_db()

            extra = ""
            if hasattr(policy, "limit_cost"):
                extra = f" limit_cost={policy.limit_cost}"
            logger.info(
                "A policy %s has fired.%s",
                policy.uuid.hex,
                extra,
            )

            for action in policy.get_immediate_actions():
                action.method(policy)
                logger.info(
                    "%s action of policy %s has been triggered.",
                    action.method.__name__,
                    policy.uuid.hex,
                )
        else:
            # Atomic CAS: only reset if has_fired is still True in DB.
            updated = (
                type(policy)
                .objects.filter(pk=policy.pk, has_fired=True)
                .update(has_fired=False, fired_datetime=timezone.now())
            )
            if updated == 0:
                continue  # Already reset by another worker

            policy.refresh_from_db()
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
