from django.apps import AppConfig


class ProposalConfig(AppConfig):
    name = 'waldur_mastermind.proposal'
    verbose_name = 'Proposal'

    def ready(self):
        pass
