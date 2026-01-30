from django.contrib.contenttypes.models import ContentType

from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Feed
from waldur_core.permissions.models import UserRole
from waldur_core.structure.digest_providers import (
    BaseDigestSectionProvider,
    DigestSectionData,
    register_provider,
)


class TeamChangesProvider(BaseDigestSectionProvider):
    section_key = "team_changes"
    section_title = "Team changes"
    section_title_key = "Team changes"
    order = 40

    def get_section_data(self, project, period_start, period_end):
        project_ct = ContentType.objects.get_for_model(project)

        # Count new members by role during the period
        joined_roles = (
            UserRole.objects.filter(
                content_type=project_ct,
                object_id=project.id,
                is_active=True,
                created__gte=period_start,
                created__lt=period_end,
            )
            .select_related("role")
            .values_list("role__description", "role__name")
        )
        joined_by_role = {}
        for description, name in joined_roles:
            role_name = description or name
            joined_by_role[role_name] = joined_by_role.get(role_name, 0) + 1

        # Count departed members by role via event log
        revoked_events = Feed.objects.filter(
            content_type=project_ct,
            object_id=project.id,
            event__event_type=EventType.ROLE_REVOKED,
            event__created__gte=period_start,
            event__created__lt=period_end,
        ).select_related("event")
        left_by_role = {}
        for feed in revoked_events:
            role_name = feed.event.context.get("role_name", "Unknown")
            left_by_role[role_name] = left_by_role.get(role_name, 0) + 1

        if not joined_by_role and not left_by_role:
            return None

        total_joined = sum(joined_by_role.values())
        total_left = sum(left_by_role.values())

        return DigestSectionData(
            key=self.section_key,
            title_key=self.section_title_key,
            order=self.order,
            context={
                "joined_by_role": joined_by_role,
                "left_by_role": left_by_role,
                "total_joined": total_joined,
                "total_left": total_left,
            },
            text_template="structure/digest_team_summary.txt",
            html_template="structure/digest_team_summary.html",
        )

    def is_available(self):
        return True


register_provider(TeamChangesProvider)
