from django.apps import AppConfig
from django.db.models import signals


class ProposalConfig(AppConfig):
    name = 'waldur_mastermind.proposal'
    verbose_name = 'Proposal'

    def ready(self):
        from waldur_mastermind.proposal import handlers, models

        signals.post_save.connect(
            handlers.create_reviews,
            sender=models.Proposal,
            dispatch_uid='waldur_mastermind.proposal.create_reviews',
        )
