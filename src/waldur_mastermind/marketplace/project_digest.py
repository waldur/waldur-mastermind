from django.utils import timezone

from waldur_core.structure.digest_providers import (
    BaseDigestSectionProvider,
    DigestSectionData,
    register_provider,
)


class ResourceUsageProvider(BaseDigestSectionProvider):
    section_key = "resource_usage"
    section_title = "Resource Usage"
    section_title_key = "Resource Usage"
    order = 10

    def get_section_data(self, project, period_start, period_end):
        from waldur_mastermind.marketplace.models import Resource

        resources = Resource.objects.filter(
            project=project,
            state=Resource.States.OK,
        ).select_related("offering")

        if not resources.exists():
            return None

        resource_list = [
            {
                "name": r.name,
                "offering_name": r.offering.name if r.offering else "",
                "state": r.get_state_display(),
            }
            for r in resources
        ]

        return DigestSectionData(
            key=self.section_key,
            title_key=self.section_title_key,
            order=self.order,
            context={
                "resources": resource_list,
                "resource_count": len(resource_list),
            },
            text_template="marketplace/digest_resource_usage.txt",
            html_template="marketplace/digest_resource_usage.html",
        )

    def is_available(self):
        return True


class EndDateProvider(BaseDigestSectionProvider):
    section_key = "end_date_info"
    section_title = "Project Timeline"
    section_title_key = "Project Timeline"
    order = 20

    def get_section_data(self, project, period_start, period_end):
        if not project.end_date:
            return None

        days_remaining = (project.end_date - timezone.now().date()).days

        return DigestSectionData(
            key=self.section_key,
            title_key=self.section_title_key,
            order=self.order,
            context={
                "end_date": project.end_date,
                "days_remaining": days_remaining,
                "is_urgent": days_remaining <= 30,
            },
            text_template="marketplace/digest_end_date.txt",
            html_template="marketplace/digest_end_date.html",
        )

    def is_available(self):
        return True


register_provider(ResourceUsageProvider)
register_provider(EndDateProvider)
