from django.core.management.base import BaseCommand

from waldur_core.structure import models as structure_models

from waldur_mastermind.experts import quotas


class Command(BaseCommand):
    help = "Recalculate experts quota usage for each project."

    def handle(self, *args, **options):
        for project in structure_models.Project.objects.all():
            quotas.update_project_quota(project)

        for customer in structure_models.Customer.objects.all():
            quotas.update_customer_quota(customer)
