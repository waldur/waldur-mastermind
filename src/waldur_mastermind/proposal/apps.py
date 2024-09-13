from django.apps import AppConfig
from django.db.models import signals


class ProposalConfig(AppConfig):
    name = "waldur_mastermind.proposal"
    verbose_name = "Proposal"

    def ready(self):
        from waldur_mastermind.proposal import models

        from . import handlers

        signals.post_save.connect(
            handlers.set_project_start_date,
            sender=models.Proposal,
            dispatch_uid="waldur_mastermind.proposal.handlers.set_project_start_date",
        )
