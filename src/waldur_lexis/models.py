import logging

from django.db import models
from django.utils.translation import gettext_lazy as _
from django_fsm import FSMIntegerField, transition
from model_utils.models import TimeStampedModel

from waldur_core.core import models as core_models
from waldur_mastermind.marketplace import models as marketplace_models

from . import backend, exceptions, structures

logger = logging.getLogger(__name__)


class LexisLink(core_models.UuidMixin, core_models.ErrorMessageMixin, TimeStampedModel):
    class States:
        PENDING = 1
        EXECUTING = 2
        OK = 3
        ERRED = 4

        CHOICES = (
            (PENDING, "pending"),
            (EXECUTING, "executing"),
            (OK, "OK"),
            (ERRED, "erred"),
        )

    robot_account = models.OneToOneField(
        to=marketplace_models.RobotAccount,
        on_delete=models.CASCADE,
        related_name="lexis_link",
    )
    state = FSMIntegerField(default=States.PENDING, choices=States.CHOICES)
    heappe_project_id = models.PositiveIntegerField(blank=True, null=True)

    class Meta:
        verbose_name = _("Lexis Link")
        ordering = ("created",)

    @property
    def human_readable_state(self):
        return str(dict(self.States.CHOICES)[self.state])

    @transition(
        field=state, source=[States.PENDING, States.ERRED], target=States.EXECUTING
    )
    def set_state_executing(self):
        pass

    @transition(field=state, source=States.EXECUTING, target=States.OK)
    def set_ok(self):
        pass

    @transition(field=state, source="*", target=States.ERRED)
    def set_erred(self):
        pass

    def get_backend(self):
        try:
            heappe_config = self.get_heappe_config()
            heappe_backend = backend.HeappeBackend(heappe_config)
            return heappe_backend
        except exceptions.HeappeConfigError as exc:
            logger.exception(exc)
            self.error_message = str(exc)
            self.set_erred()
            self.save(update_fields=["error_message", "state"])

    def get_heappe_config(self):
        offering = self.robot_account.resource.offering
        heappe_url = offering.plugin_options.get("heappe_url")
        if heappe_url is None:
            raise exceptions.HeappeConfigError(
                "Offering %s does not include heappe_url option" % offering
            )

        heappe_username = offering.plugin_options.get("heappe_username")
        if heappe_username is None:
            raise exceptions.HeappeConfigError(
                "Offering %s does not include heappe_username option" % offering
            )

        heappe_password = offering.secret_options.get("heappe_password")
        if heappe_password is None:
            raise exceptions.HeappeConfigError(
                "Offering %s does not include heappe_password option" % offering
            )

        heappe_cluster_id = offering.plugin_options.get("heappe_cluster_id")
        if heappe_cluster_id is None:
            raise exceptions.HeappeConfigError(
                "Offering %s does not include heappe_cluster_id option" % offering
            )

        heappe_local_base_path = offering.plugin_options.get("heappe_local_base_path")
        if heappe_local_base_path is None:
            raise exceptions.HeappeConfigError(
                "Offering %s does not include heappe_local_base_path option" % offering
            )

        heappe_cluster_password = offering.secret_options.get("heappe_cluster_password")

        return structures.HeappeConfig(
            heappe_url=heappe_url,
            heappe_username=heappe_username,
            heappe_password=heappe_password,
            heappe_cluster_id=heappe_cluster_id,
            heappe_local_base_path=heappe_local_base_path,
            heappe_cluster_password=heappe_cluster_password,
        )

    def __str__(self) -> str:
        return "Lexis link {} <-> {} ({})".format(
            self.robot_account.username,
            self.robot_account.resource.name,
            self.human_readable_state,
        )
