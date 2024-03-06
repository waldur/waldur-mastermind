import datetime

import requests
from django.core.management.base import BaseCommand

from waldur_core.core import utils as core_utils
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models

# Waldur usage to LUMI usage
usage_type_mapping = {
    "cpu_hours": "cpu_k_hours",
    "gpu_hours": "gpu_hours",
    "terabyte_hours": "gb_k_hours",
}


class Command(BaseCommand):
    help = """
    Import component usages from Reppu for a specified year and month.
    """

    dry_run = False

    def get_reppu_usages(self, reppu_api_url, reppu_api_token, start_time, end_time):
        self.stdout.write(
            self.style.SUCCESS(
                f"Processing Reppu usages from {start_time} to {end_time}"
            )
        )
        headers = {
            "Authorization": f"Bearer {reppu_api_token}",
        }
        start_time_str = start_time.isoformat()
        end_time_str = end_time.isoformat()
        payload = {
            "query": "query LumiUsage($startTime: String!, $endTime: String!) { lumiUsage(startTime: $startTime, endTime: $endTime) {cn, puhuriuuid, cpu_hours, gpu_hours, terabyte_hours}}",
            "variables": {"startTime": start_time_str, "endTime": end_time_str},
        }

        response = requests.post(reppu_api_url, json=payload, headers=headers)

        if response.status_code != 200:
            self.stdout.write(
                self.style.ERROR(
                    f"Unable to fetch usages: status code {response.status_code}, {response.json()}"
                )
            )
            return

        return response.json()

    def process_usages(self, lumi_usages, year, month):
        for lumi_usage in lumi_usages:
            project_uuid = lumi_usage["puhuriuuid"]
            project = structure_models.Project.objects.filter(uuid=project_uuid).first()
            if project is None:
                self.stdout.write(
                    self.style.ERROR(
                        f"There are not project with uuid {project_uuid}, skipping processing."
                    )
                )
                continue

            self.stdout.write(
                self.style.SUCCESS(
                    f"Processing project {project}",
                )
            )

            resources = marketplace_models.Resource.objects.filter(
                project=project,
            )

            if resources.count() == 0:
                self.stdout.write(
                    self.style.ERROR(
                        f"The project {project} does not have any resources, skipping processing"
                    )
                )
                continue

            self.import_usages(lumi_usage, resources, month, year)

    def set_resource_usage(
        self,
        resource: marketplace_models.Resource,
        usage_type: str,
        new_usage: float,
        month: int,
        year: int,
        one_of_many: bool = False,
    ):
        self.stdout.write(
            self.style.SUCCESS(
                f"Processing resource {resource}",
            )
        )

        component_usage = resource.usages.objects.filter(
            type=usage_type,
            start_date__month=month,
            start_date__year=year,
        ).first()

        if component_usage is None:
            self.stdout.write(
                self.style.WARNING(
                    f"The resource {resource} does not have any component usages with type {usage_type}, skipping processing"
                )
            )
            return

        if one_of_many:
            new_usage = min(new_usage, component_usage.component.limit_amount)

        self.stdout.write(
            self.style.SUCCESS(
                f"Setting {resource} {usage_type} component usage from {component_usage.usage} to {new_usage}"
            )
        )

        if self.dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    f"[Dry run] Setting {resource} {usage_type} component usage from {component_usage.usage} to {new_usage}"
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Setting {resource} {usage_type} component usage from {component_usage.usage} to {new_usage}"
                )
            )
            component_usage.usage = new_usage
            component_usage.save(update_fields=["usage"])

        return new_usage

    def import_usages(self, lumi_usage, resources, month, year):
        # The cycle iterates over component usages fetched from lumi
        for lumi_usage_type, lumi_usage_amount in lumi_usage.items():
            # Ignore unknown component types
            if lumi_usage_type not in usage_type_mapping and lumi_usage_type not in [
                "puhuriuuid",
                "cn",
            ]:
                self.stdout.write(
                    self.style.WARNING(
                        f"Unknown component type {lumi_usage_type} for {resources.first().project}"
                    )
                )
                continue

            waldur_usage_type = usage_type_mapping[lumi_usage_type]
            # Convert CPU hours to CPU Khours
            if waldur_usage_type == "cpu_k_hours":
                lumi_usage_amount /= 1000

            lumi_usage_amount_rounded = round(lumi_usage_amount, 2)
            if resources.count() > 1:
                resource = resources.first()
                self.set_resource_usage(
                    resource, waldur_usage_type, lumi_usage_amount_rounded, month, year
                )
            else:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Processing {resources.count()} resources in {resource.project}",
                    )
                )
                resources = resources.order_by("created")

                total_usage = lumi_usage_amount_rounded
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Total {lumi_usage_type} usage: {lumi_usage_amount_rounded}",
                    )
                )
                for resource in resources:
                    usage_set = self.set_resource_usage(
                        resource, waldur_usage_type, total_usage, month, year, True
                    )
                    if usage_set:
                        total_usage -= usage_set

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            "-m",
            "--month",
            type=int,
            dest="month",
            help="Month for which data is imported.",
        )

        parser.add_argument(
            "-y",
            "--year",
            type=int,
            dest="year",
            help="Year for which data is imported.",
        )
        parser.add_argument(
            "--reppu-api-url",
            type=str,
            dest="reppu_api_url",
            help="Reppu API URL.",
        )
        parser.add_argument(
            "--reppu-api-token",
            type=str,
            dest="reppu_api_token",
            help="Reppu API Token.",
        )
        parser.add_argument(
            "--dry-run",
            type=bool,
            dest="dry_run",
            default=False,
            help="Dry run mode.",
        )

    def handle(self, *args, **options):
        year = options.get("year")
        if year is None:
            self.stdout.write(self.style.ERROR("Year value is empty."))
            return

        month = options.get("month")
        if month is None:
            self.stdout.write(self.style.ERROR("Month value is empty."))
            return

        reppu_api_url = options.get("reppu_api_url")
        if reppu_api_url is None:
            self.stdout.write(self.style.ERROR("Reppu API URL value is empty."))
            return

        reppu_api_token = options.get("reppu_api_token")
        if reppu_api_token is None:
            self.stdout.write(self.style.ERROR("Reppu API Token is empty."))
            return

        self.dry_run = options.get("dry_run", False)

        date = datetime.date(year=year, month=month, day=1)
        start_date = core_utils.month_start(date).astimezone(datetime.UTC)
        end_date = core_utils.month_end(date).astimezone(
            datetime.UTC
        ) + datetime.timedelta(seconds=1)

        reppu_usage_data = self.get_reppu_usages(
            reppu_api_url, reppu_api_token, start_date, end_date
        )

        if reppu_usage_data is None:
            exit(1)

        lumi_usages = reppu_usage_data["data"]["lumiUsage"]

        self.process_usages(lumi_usages, year, month)
