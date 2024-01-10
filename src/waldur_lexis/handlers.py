import logging

from waldur_mastermind.marketplace import models as marketplace_models

from . import executors, models

logger = logging.getLogger(__name__)


def request_ssh_key_for_heappe_robot_account(
    sender, instance: marketplace_models.RobotAccount, created=False, **kwargs
):
    if created:
        return

    if not instance.type.startswith("hl"):
        return

    try:
        lexis_link = instance.lexis_link
    except models.LexisLink.DoesNotExist:
        logger.error(
            "The robot account %s doesn't have a related lexis link, skipping ssh key request",
            instance,
        )
        return

    if lexis_link.state not in [
        models.LexisLink.States.PENDING,
        models.LexisLink.States.ERRED,
    ]:
        logger.error("%s has incorrect state, skipping ssh key request", lexis_link)
        return

    if (
        instance.tracker.previous("username") != ""
        or not instance.tracker.has_changed("username")
        or instance.username == ""
    ):
        logger.error("The username of the robot account %s is already set", instance)
        return

    if instance.resource.backend_id in [None, ""]:
        logger.error(
            "The backend_id of resource %s is empty, skipping ssh key request",
            instance.resource,
        )
        return

    logger.info("Requesting SSH key for %s", lexis_link)
    lexis_link.set_state_executing()
    lexis_link.save()

    executors.SshKeyCreateExecutor().execute(lexis_link)
