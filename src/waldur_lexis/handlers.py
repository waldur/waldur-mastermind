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

    # This should only process if RobotAccount gets the OK state
    if instance.state != marketplace_models.RobotAccount.States.OK:
        return

    try:
        lexis_link = instance.lexis_link
    except models.LexisLink.DoesNotExist:
        logger.info(
            "The robot account %s doesn't have a related lexis link, skipping ssh key request",
            instance,
        )
        return

    # if a linked Lexis link exists and it has the PENDING state, only then we should start LexisLink processing
    if lexis_link.state != models.LexisLink.States.PENDING:
        logger.info(
            "The lexis link %s is not in PENDING state, skipping ssh key request",
            lexis_link,
        )
        return

    if instance.username == "":
        logger.error("The username of the robot account %s is empty", instance)
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
