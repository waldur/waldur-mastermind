import argparse
import datetime

import requests
from django.core.management.base import BaseCommand

from waldur_core.core import utils as core_utils
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models

# Waldur usage to LUMI usage
component_type_mapping = {
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
        component_type: str,
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

        component_usage = resource.usages.filter(
            component__type=component_type,
            billing_period__month=month,
            billing_period__year=year,
        ).first()

        if component_usage is None:
            self.stdout.write(
                self.style.WARNING(
                    f"The resource {resource} does not have any component usages with type {component_type}, skipping processing"
                )
            )
            return

        if one_of_many:
            resource_limit = resource.limits.get(component_type)
            if resource_limit is None:
                self.stdout.write(
                    self.style.WARNING(
                        f"The resource does not have limits for {component_type}, skipping processing.",
                    )
                )
                return
            new_usage = min(new_usage, resource_limit)

        self.stdout.write(
            self.style.SUCCESS(
                f"Setting {resource} {component_type} component usage from {component_usage.usage} to {new_usage}"
            )
        )

        if self.dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    f"[Dry run] Setting {resource} {component_type} component usage from {component_usage.usage} to {new_usage}"
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Setting {resource} {component_type} component usage from {component_usage.usage} to {new_usage}"
                )
            )
            component_usage.usage = new_usage
            component_usage.save(update_fields=["usage"])

        return new_usage

    def import_usages(self, lumi_usage, resources, month, year):
        # The cycle iterates over component usages fetched from lumi
        for lumi_component_type, lumi_usage_amount in lumi_usage.items():
            # Ignore metadata
            if lumi_component_type in [
                "puhuriuuid",
                "cn",
            ]:
                continue

            # Ignore unknown component types
            if lumi_component_type not in component_type_mapping:
                self.stdout.write(
                    self.style.WARNING(
                        f"Unknown component type {lumi_component_type} for {resources.first().project}"
                    )
                )
                continue

            waldur_component_type = component_type_mapping[lumi_component_type]
            # Convert CPU hours to CPU Khours
            if waldur_component_type == "cpu_k_hours":
                lumi_usage_amount /= 1000

            lumi_usage_amount_rounded = round(lumi_usage_amount, 2)
            if resources.count() == 1:
                resource = resources.first()
                self.set_resource_usage(
                    resource,
                    waldur_component_type,
                    lumi_usage_amount_rounded,
                    month,
                    year,
                    one_of_many=False,
                )
            else:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Processing {resources.count()} resources in {resources.first().project}",
                    )
                )
                resources = resources.order_by("created")

                total_usage = lumi_usage_amount_rounded
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Total {lumi_component_type} usage: {lumi_usage_amount_rounded}",
                    )
                )
                for index, resource in enumerate(resources):
                    # Last resource usage filled with total usage leftover
                    if index == resources.count() - 1:
                        self.set_resource_usage(
                            resource,
                            waldur_component_type,
                            total_usage,
                            month,
                            year,
                            one_of_many=False,
                        )
                    else:
                        usage_set = self.set_resource_usage(
                            resource,
                            waldur_component_type,
                            total_usage,
                            month,
                            year,
                            one_of_many=True,
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
            action=argparse.BooleanOptionalAction,
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

        if self.dry_run:
            self.stdout.write(self.style.SUCCESS("Running in dry-run mode."))

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
