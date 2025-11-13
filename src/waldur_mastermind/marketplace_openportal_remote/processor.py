import logging

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import processors
from waldur_openportal import models as openportal_models
from waldur_openportal import views as openportal_views

logger = logging.getLogger(__name__)


class CreateRemoteAllocationProcessor(processors.BaseCreateResourceProcessor):
    viewset = openportal_views.RemoteAllocationViewSet

    fields = ("name", "description", "allocation")

    def validate_order(self, request):
        # we need to use a default name if the user hasn't provided one
        attributes = request.data.get("attributes", {})

        name = attributes.get("name", "").strip()

        if not name:
            attributes["name"] = "Remote Allocation"

            # use the name of the offering as the default
            try:
                offering_uuid = request.data.get("offering", "").split("/")[-2]
                offering = marketplace_models.Offering.objects.get(uuid=offering_uuid)
                attributes["name"] = offering.name
                logger.info(
                    f"Setting default name for remote allocation: {attributes['name']}"
                )
            except Exception as e:
                logger.error(f"Failed to set default name for remote allocation: {e}")

            request.data["attributes"] = attributes

        super().validate_order(request)


class DeleteRemoteAllocationProcessor(processors.DeleteScopedResourceProcessor):
    viewset = openportal_views.RemoteAllocationViewSet


class UpdateRemoteAllocationLimitsProcessor(processors.BasicUpdateResourceProcessor):
    def update_limits_process(self, user):
        allocation: openportal_models.RemoteAllocation = self.order.resource.scope
        allocation.schedule_updating()
        allocation.save(update_fields=["state"])

        return False
