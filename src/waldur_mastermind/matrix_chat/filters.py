import django_filters
from django.contrib.contenttypes.models import ContentType

from waldur_core.core import filters as core_filters
from waldur_core.structure.models import Project

from . import models


class MatrixRoomFilter(django_filters.FilterSet):
    project_uuid = core_filters.RelatedUUIDFilter(
        view_name="project-detail",
        method="filter_by_project_uuid",
        label="Project UUID",
    )
    state = django_filters.ChoiceFilter(choices=models.RoomStates.CHOICES)
    member = django_filters.BooleanFilter(
        method="filter_by_member",
        label="Only rooms the current user is a member of",
    )

    class Meta:
        model = models.MatrixRoom
        fields = []

    def filter_by_project_uuid(self, queryset, name, value):
        ct = ContentType.objects.get_for_model(Project)
        return queryset.filter(
            content_type=ct,
            object_id__in=Project.objects.filter(uuid=value).values_list(
                "id", flat=True
            ),
        )

    def filter_by_member(self, queryset, name, value):
        # Used by the user-facing chat list to show only conversations the
        # caller belongs to. The admin room view omits the param and keeps
        # seeing every room.
        if not value:
            return queryset
        user = self.request.user
        if not user.is_authenticated:
            return queryset.none()
        return queryset.filter(
            members__user=user,
            members__membership_state__in=[
                models.MembershipStates.INVITED,
                models.MembershipStates.JOINED,
            ],
        )


class MatrixHistoryExportFilter(django_filters.FilterSet):
    room_uuid = core_filters.RelatedUUIDFilter(
        view_name="matrix-room-detail",
        field_name="room__uuid",
        label="Room UUID",
    )
    state = django_filters.ChoiceFilter(choices=models.ExportStates.CHOICES)
    export_type = django_filters.ChoiceFilter(choices=models.ExportTypes.CHOICES)

    class Meta:
        model = models.MatrixHistoryExport
        fields = []
