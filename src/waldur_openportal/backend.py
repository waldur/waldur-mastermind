import decimal
import json
import logging
import re

import openportal
from django.conf import settings as django_settings
from django.db import transaction

from waldur_core.core import utils as core_utils
from waldur_core.core.enums import CoreStates
from waldur_core.structure.backend import ServiceBackend
from waldur_core.structure.exceptions import ServiceBackendError
from waldur_openportal import signals
from waldur_openportal.client import OpenPortalClient

from . import exceptions, models
from . import utils as openportal_utils

logger = logging.getLogger(__name__)


class OpenPortalBackend(ServiceBackend):
    def __init__(self, settings):
        self.settings = settings

    @property
    def client(self) -> OpenPortalClient:
        """
        Lazy initialize OpenPortal client instance
        """
        if not hasattr(self, "_client"):
            self._client = self.get_client(self.settings)
        return self._client

    def destination(self) -> openportal.Destination:
        """
        Return the OpenPortal Destination for the instance
        being managed by this backend
        """
        return self.client.destination()

    def portal(self) -> openportal.PortalIdentifier:
        """
        Return the OpenPortal Portal for the instance
        being managed by this backend
        """
        return self.client.portal()

    def get_client(self, settings):
        return OpenPortalClient(
            instance_name=settings.options.get("instance_name", None),
        )

    def pull_resources(self):
        logger.debug(f"Pulling OpenPortal resources for settings: {self}")

        logger.warning("Skipping pull_resources")

    def ping(self, raise_exception=False):
        logger.debug("Pinging OpenPortal")
        try:
            self.client.health()
        except exceptions.OpenPortalError as e:
            logger.error(f"OpenPortal is not available: {e}")
            if raise_exception:
                raise ServiceBackendError(e)
            return False
        else:
            return True

    def get_project_shortname(self, project):
        """
        Return the preferred shortname for the passed project.
        """
        return openportal_utils.get_project_shortname(project)

    def get_user_shortname(self, user):
        """
        Return the preferred shortname for the passed user.
        """
        return openportal_utils.get_user_shortname(user)

    def sync_users(self, allocation: models.Allocation) -> None:
        if not isinstance(allocation, models.Allocation):
            raise ServiceBackendError("Invalid allocation type %s" % type(allocation))

        if not (
            allocation.has_project_identifier() or allocation.is_added_to_openportal()
        ):
            logger.warning(
                f"Allocation {allocation} is not in OpenPortal - adding now!"
            )
            # this already calls 'sync_users' on the created allocation
            self.add_allocated_project(allocation)
            return

        project = allocation.get_project_identifier()
        logger.debug(f"Syncing users for allocation: {allocation} | {project}")
        users = allocation.project.get_users()
        logger.debug(f"Users for allocation: {users}")

        # list all users who OpenPortal thinks are in the project
        user_mappings = self.client.get_users(project)

        logger.debug(f"Users of {project} in OpenPortal: {user_mappings}")

        allocated_mappings = []

        # go through and add the users who are not in OpenPortal
        for user in users:
            if not user.is_active:
                logger.warning(
                    f"Removing {user} as they are no longer listed as active"
                )
                continue

            # get the association between the user and the allocation
            try:
                (association, _) = models.Association.objects.get_or_create(
                    user=user, allocation=allocation
                )
            except models.Association.MultipleObjectsReturned:
                association = openportal_utils.get_association(
                    user=user, allocation=allocation
                )

            mapping = None

            if association.has_mapping():
                mapping = association.get_mapping()

            try:
                if mapping is None or mapping not in user_mappings:
                    logger.info(f"Adding user {user} to OpenPortal")
                    shortname = self.get_user_shortname(user)

                    if shortname is None or not shortname.strip():
                        logger.error(
                            f"Empty shortname for user: {user} - cannot add to OpenPortal"
                        )
                        continue

                    new_mapping = self.client.add_user(
                        shortname=shortname, project=project
                    )

                    logger.debug(
                        f"Added user {user} to OpenPortal project {project} with mapping {new_mapping}"
                    )

                    if (mapping is not None) and (new_mapping != mapping):
                        logger.warning(
                            f"User {user} has a changing username in OpenPortal: {mapping} -> {new_mapping}"
                        )

                    mapping = new_mapping

                    association.set_mapping(mapping)
                    association.save()

                    signals.openportal_association_created.send(
                        models.Allocation,
                        allocation=allocation,
                        user=user,
                    )

                allocated_mappings.append(mapping)
            except Exception as e:
                logger.error(f"Unable to add user {user} to OpenPortal: {e}")

                if mapping is not None:
                    # make sure we keep any existing mapping, so that we don't flap
                    # back and forth trying to delete an existing user who we failed
                    # to add
                    logger.warning(f"Keeping existing user with mapping {mapping}")
                    allocated_mappings.append(mapping)

        stale_mappings = [
            mapping for mapping in user_mappings if mapping not in allocated_mappings
        ]

        if len(stale_mappings) > 0:
            logger.debug(f"Stale users in OpenPortal: {stale_mappings}")

        for mapping in stale_mappings:
            try:
                self.client.delete_user(mapping.user)
                # no need to signal as the user has already been removed from the association
            except Exception as e:
                logger.error(
                    f"Unable to delete user with mapping {mapping} from OpenPortal: {e}"
                )

    def assert_can_create_allocation_for_project(self, project):
        """
        This checks to see if the passed project is allowed to create an allocation
        on the instance managed by this backend. Projects are only allowed to create
        a single allocation per instance, and they must have a routing path
        that matches the destination of this instance.
        """
        destination = str(self.client.destination())

        logger.debug(
            f"Asserting that project {project} can create an allocation for {destination}"
        )

        existing_allocations = self.get_allocation_queryset().filter(project=project)

        # find all of these allocations that are active and that have a project identifier
        existing_allocations = [
            allocation
            for allocation in existing_allocations
            if allocation.has_project_identifier()
            and allocation.state != CoreStates.ERRED
        ]

        if len(existing_allocations) > 0:
            logger.error(
                f"Project {project} already has existing allocation(s) in OpenPortal for {destination}"
            )
            logger.error(f"These are {existing_allocations}")
            raise ServiceBackendError(
                f"Project {project} already has an allocation for {destination} in OpenPortal. "
                + "You may only have a single active allocation per destination per project. "
                + f"The existing allocation(s) are: {existing_allocations}"
            )

        # now look at the allowed destinations for this project, from its
        # project-info object
        project_info, created = models.ProjectInfo.objects.get_or_create(
            project=project
        )
        project_info.sanitise()

        if (
            project_info.allowed_destinations is None
            or len(project_info.allowed_destinations.strip()) == 0
        ):
            # by default, projects can connect to any destination - this can be refined
            # down as needed by the admin
            logger.debug(
                f"Project {project} is allowed to create an allocation for any destination"
            )
            return

        allowed_destinations = project_info.allowed_destinations.split(",")

        for allowed_destination in allowed_destinations:
            allowed_destination = allowed_destination.strip()

            # the allowed_destination is a regular expression, so we need to match it
            if allowed_destination == destination:
                # we have an exact match
                return
            elif allowed_destination == "*":
                # this is a wildcard, so we allow it
                return
            else:
                if re.match(allowed_destination, destination):
                    # this is a match
                    return

        logger.error(
            f"Project {project} is not allowed to create an allocation for {destination}"
        )
        logger.error(f"Allowed destinations are: {allowed_destinations}")

        raise ServiceBackendError(
            f"Project {project} is not allowed to create an allocation for {destination}. "
            + f"Allowed destinations are: {allowed_destinations}"
        )

    def _add_allocated_project(
        self, allocation: models.Allocation
    ) -> models.Allocation:
        if not isinstance(allocation, models.Allocation):
            raise ServiceBackendError("Invalid allocation type %s" % type(allocation))

        self.assert_can_create_allocation_for_project(allocation.project)

        if allocation.has_project_identifier():
            project = allocation.get_project_identifier()
            logger.debug(f"Allocation already exists: {allocation} | {project}")

            # add it again just to be sure
            try:
                mapping = self.client.add_project(project)
            except Exception as e:
                logger.warning(
                    f"Unable to re-add project {project} to OpenPortal: {e}. This will be re-added later..."
                )
                return allocation

            logger.debug(
                f"Re-added allocation {allocation} to OpenPortal with mapping {mapping}"
            )
            allocation.is_added = True

            if allocation.has_mapping():
                if allocation.get_mapping() != mapping:
                    logger.warning(
                        f"Allocation {allocation} has a changing project name in OpenPortal: {mapping} -> {allocation.get_mapping()}"
                    )
                else:
                    allocation.set_mapping(mapping)
        else:
            project = allocation.project
            project_name = self.get_project_shortname(project)

            logger.debug(
                f"Creating allocation: {allocation} for project {project_name}"
            )

            if project_name is None or not project_name.strip():
                logger.error(
                    f"Empty project_name for allocation: {allocation} - cannot create in OpenPortal"
                )
                raise ServiceBackendError(
                    f"Empty project_name for allocation. Please set a short name for {project}"
                )

            try:
                mapping = self.client.add_project(project_name)
            except Exception as e:
                logger.warning(
                    f"Unable to create OpenPortal project {project_name}: {e}. This will be created later..."
                )
                return allocation

            logger.debug(
                f"Created OpenPortal project {project_name} with mapping {mapping}"
            )
            allocation.set_mapping(mapping)
            allocation.is_added = True

        allocation.save()
        return allocation

    def add_allocated_project(self, allocation: models.Allocation) -> models.Allocation:
        allocation = self._add_allocated_project(allocation)

        logger.debug(f"Allocation node limit is {allocation.node_limit}")

        if allocation.has_project_identifier():
            if allocation.is_added_to_openportal():
                self.sync_users(allocation)
            else:
                logger.warning(
                    f"Allocation {allocation} is not yet in OpenPortal - not syncing users yet"
                )
        else:
            logger.error(
                f"Unable to create OpenPortal allocation for {allocation}: This will be created later..."
            )
            raise ServiceBackendError(
                f"Unable to create OpenPortal allocation for {allocation}"
            )

        return allocation

    def check_added_allocation(
        self, allocation: models.Allocation
    ) -> models.Allocation:
        if not isinstance(allocation, models.Allocation):
            raise ServiceBackendError("Invalid allocation type %s" % type(allocation))

        if allocation.has_project_identifier() and allocation.is_added_to_openportal():
            # all good
            return allocation

        # only try to add the project if the allocation is still valid.
        # We don't want to get stuck in a loop continually trying
        # to add an allocation that has been deleted
        if allocation.state not in [
            CoreStates.CREATION_SCHEDULED,
            CoreStates.CREATING,
            CoreStates.UPDATE_SCHEDULED,
            CoreStates.UPDATING,
            CoreStates.OK,
        ]:
            logger.warning(
                f"Allocation {allocation} is in state {allocation.state} - cannot add to OpenPortal"
            )
            raise ServiceBackendError(
                f"Allocation {allocation} is in state {allocation.state} - cannot add to OpenPortal"
            )

        allocation = self._add_allocated_project(allocation)

        if not (
            allocation.has_project_identifier() and allocation.is_added_to_openportal()
        ):
            logger.error(
                f"Allocation {allocation} could not be added to OpenPortal. Try again later."
            )
            raise ServiceBackendError(
                f"Allocation {allocation} could not be added to OpenPortal. Try again later."
            )

        return allocation

    def create_allocation(self, allocation):
        if not isinstance(allocation, models.Allocation):
            raise ServiceBackendError("Invalid allocation type %s" % type(allocation))

        allocation = self._add_allocated_project(allocation)

        default_limits = django_settings.WALDUR_OPENPORTAL["DEFAULT_LIMITS"]
        allocation.node_limit = decimal.Decimal(default_limits["NODE"])
        allocation.save()

        if allocation.has_project_identifier():
            if allocation.is_added_to_openportal():
                # schedule syncing users as a background task so that we don't block the Waldur GUI
                # If this fails, then another sync process will fix things later
                from . import tasks

                tasks.sync_allocation_users.delay(
                    core_utils.serialize_instance(allocation)
                )
            else:
                logger.warning(
                    f"Allocation {allocation} for project {allocation.project} is not in OpenPortal - cannot sync users"
                )
        else:
            logger.warning(
                f"Allocation {allocation} for project {allocation.project} has no project identifier - will try again later..."
            )

    def delete_allocation(self, allocation):
        if not isinstance(allocation, models.Allocation):
            raise ServiceBackendError("Invalid allocation type %s" % type(allocation))

        logger.info(f"Deleting allocation: {allocation}")

        if not (
            allocation.has_project_identifier() or allocation.is_added_to_openportal()
        ):
            logger.debug(f"Allocation already deleted: {allocation}")
        else:
            try:
                project = allocation.get_project_identifier()
                self.client.delete_project(project)
                allocation.is_added = False
                allocation.save()
            except Exception as e:
                logger.error(
                    f"Unable to delete allocation {allocation} from OpenPortal: {e}"
                )

    def add_user(self, allocation: models.Allocation, user) -> bool:
        """
        Create association between user and OpenPortal account if it does not exist yet.
        The allocation contains the information of which project the user is in.
        """
        if not isinstance(allocation, models.Allocation):
            raise ServiceBackendError("Invalid allocation type %s" % type(allocation))

        if not allocation.is_added_to_openportal():
            # try to add now
            allocation = self.add_allocated_project(allocation)

            if not allocation.is_added_to_openportal():
                logger.error(
                    f"Allocation {allocation} is not in OpenPortal - cannot add user {user}"
                )
                return False

        if not allocation.has_project_identifier():
            logger.error(
                f"Allocation {allocation} has no project identifier - cannot add user {user} to OpenPortal"
            )
            return False

        project = allocation.get_project_identifier()

        logger.debug(f"Adding user {user} to project {project} in OpenPortal")

        shortname = self.get_user_shortname(user)

        if shortname is None or not shortname.strip():
            logger.error(
                f"Empty shortname for user: {user} - they cannot be added to OpenPortal"
            )
            return False

        # get or create the association between the user and the allocation
        # This association holds the username of the user in OpenPortal on this instance
        try:
            (association, _) = models.Association.objects.get_or_create(
                user=user, allocation=allocation
            )
        except models.Association.MultipleObjectsReturned:
            association = openportal_utils.get_association(
                user=user, allocation=allocation
            )

        mapping = None

        if association.has_mapping():
            mapping = association.get_mapping()

        if mapping is not None:
            logger.debug(
                f"User {user} has previously been in {project} with mapping {mapping}"
            )
            logger.debug("Re-adding them to OpenPortal with the same mapping.")

        try:
            new_mapping = self.client.add_user(shortname=shortname, project=project)
            logger.debug(f"Added user {user} with mapping {new_mapping}")

            if (mapping is not None) and (new_mapping != mapping):
                logger.warning(
                    f"User {user} has a changing mapping in OpenPortal: {mapping} -> {new_mapping}"
                )

            association.set_mapping(new_mapping)
            association.save()

            signals.openportal_association_created.send(
                models.Allocation,
                allocation=allocation,
                user=user,
            )
        except Exception as e:
            logger.error(
                f"Unable to add user {user} to allocation {allocation} in OpenPortal: {e}"
            )
            return False

        return True

    def delete_user(self, allocation: models.Allocation, user) -> bool:
        """
        Delete association between user and OpenPortal account if it exists.
        """
        if not isinstance(allocation, models.Allocation):
            raise ServiceBackendError("Invalid allocation type %s" % type(allocation))

        if not allocation.is_added_to_openportal():
            logger.error(
                f"Allocation {allocation} is not in OpenPortal - cannot delete user {user}"
            )
            return False

        if not allocation.has_project_identifier():
            logger.error(
                f"Allocation {allocation} has no project identifier - cannot delete user {user} from OpenPortal"
            )
            return False

        project = allocation.get_project_identifier()

        logger.info(f"Deleting OpenPortal user {user} from project {project}")

        # find the association between the user and the allocation
        try:
            association = openportal_utils.get_association(
                user=user, allocation=allocation
            )
        except Exception as e:
            logger.error(
                f"Unable to find association between user {user} and allocation {allocation}: {e}"
            )
            return False

        if not association.has_mapping():
            logger.warning(f"User {user} is not associated with OpenPortal?")
            return False

        op_user = association.get_user_identifier()

        try:
            logger.info(f"Deleting user {op_user} from project {project} in OpenPortal")

            try:
                self.client.delete_user(op_user)
            except Exception as e:
                logger.error(
                    f"Unable to delete user {op_user} from project {project} in OpenPortal: {e}"
                )

                # see if this user still exists in the project - if not, we can continue
                mappings = self.client.get_users(project)

                if association.get_mapping() in mappings:
                    logger.error(
                        f"User {op_user} still exists in project {project} - cannot delete"
                    )
                    return False

            # delete this association
            association.delete()

            signals.openportal_association_deleted.send(
                models.Allocation, allocation=allocation, user=user
            )

            return True
        except Exception as e:
            logger.error(
                f"Unable to delete user {user} from allocation {allocation} in OpenPortal: {e}"
            )
            return False

    def set_resource_limits(self, allocation: models.Allocation):
        if not isinstance(allocation, models.Allocation):
            raise ServiceBackendError("Invalid allocation type %s" % type(allocation))

        allocation = self.check_added_allocation(allocation)

        if not allocation.has_project_identifier():
            logger.error(
                f"Allocation {allocation} has no project identifier - cannot set resource limits"
            )
            return

        project = allocation.get_project_identifier()

        limit = openportal.Usage.from_hours(allocation.node_limit)

        logger.debug(f"Setting resource limit for allocation {project} to {limit}")
        set_limit = self.client.set_resource_limits(project, limit)

        if set_limit.seconds != limit.seconds:
            logger.error(
                f"Unable to set limit for project {project} to {limit} - got {set_limit}"
            )

    def get_resource_limits(
        self, project: openportal.ProjectIdentifier
    ) -> openportal.Usage:
        logger.debug(f"Getting OpenPortal limits for account: {project}")
        limit = self.client.get_resource_limits(project)
        logger.debug(f"OpenPortal limits for project {project}: {limit}")
        return limit

    def _update_usage_from_report(
        self,
        allocation,
        report: openportal.ProjectUsageReport,
        update_current: bool = True,
    ):
        # this will be the total usage this month - check that we have
        # dates that are all in the same month...
        if len(report.dates) == 0:
            logger.error(f"Empty usage report for {allocation}")
            return

        day = report.dates[0]

        for date in report.dates[1:]:
            if date.month != day.month or date.year != day.year:
                logger.error(f"Usage report for {allocation} spans multiple months")
                return

        if report.is_complete:
            logger.debug(f"Forced update as usage report for {allocation} is complete")
        else:
            delta = float(allocation.node_usage) - float(report.total_usage.hours)

            # usage is a decimal with 2 d.p. - only changes of more than 0.01 are significant
            if abs(delta) < 0.015:
                logger.debug(
                    f"Usage for {allocation} changed by {delta} hours. This is too small to consider updating."
                )
                return

            logger.debug(
                f"Usage for {allocation} changed by {delta} hours - updating accounts..."
            )

        if update_current:
            # only update this month's usage if we are updating the current month
            allocation.node_usage = report.total_usage.hours
            allocation.save(update_fields=["node_usage"])

            # TODO - check if we need to update anything missed during a change of month?

        associations = models.Association.objects.filter(allocation=allocation)

        for association in associations:
            user = association.user

            if not association.has_user_identifier():
                continue

            user_identifier = association.get_user_identifier()

            # look up the usage for this user from the report - record this in node-hours
            try:
                usage = report.usage(user_identifier).hours
            except Exception as e:
                logger.warning(f"User {user} has no usage in the report: {e}")
                usage = 0

            # we save usage using the UserIdentifier rather than the local
            # username, so that a consistent identifier is used across
            # all resources in a project
            models.AllocationUserUsage.objects.update_or_create(
                allocation=allocation,
                year=day.year,
                month=day.month,
                user=user,
                username=str(user_identifier),
                defaults={"node_usage": usage},
            )

    def sync_usage(self, allocation: models.Allocation):
        if not isinstance(allocation, models.Allocation):
            raise ServiceBackendError("Invalid allocation type %s" % type(allocation))

        if not allocation.is_added_to_openportal():
            allocation = self.add_allocated_project(allocation)

            if not allocation.is_added_to_openportal():
                logger.error(
                    f"Allocation {allocation} is not in OpenPortal - cannot sync usage"
                )
                return

        if not allocation.has_project_identifier():
            logger.error(
                f"Allocation {allocation} has no project identifier - cannot sync usage"
            )
            return

        project = allocation.get_project_identifier()
        logger.debug(
            f"Syncing OpenPortal usage for allocation {allocation} and project {project}"
        )

        # accounting is based on collecting monthly reports - make sure to do this
        # month and last month, just in case we missed any data from the month
        # changeover
        months = [openportal.DateRange.last_month(), openportal.DateRange.this_month()]

        logger.debug(f"Months to get accounts: {months}")

        for month in months:
            # get the historical report for this month
            first_day = month.days[0]

            historical_report, created = (
                models.HistoricalAllocation.objects.get_or_create(
                    allocation=allocation,
                    year=first_day.year,
                    month=first_day.month,
                    defaults={
                        "node_usage": 0,
                        "is_complete": False,
                    },
                )
            )

            if created:
                logger.debug(f"Created historical report for {allocation} in {month}")

            is_current_month: bool = month == openportal.DateRange.this_month()

            # The current month's report cannot be complete
            is_report_complete: bool = (
                not is_current_month
            ) and historical_report.is_complete

            if is_report_complete:
                logger.debug(f"Skipping {month} as report is complete")
                continue

            report = self.client.get_usage_report(project, month)

            logger.debug(f"Total usage for project in {month} = {report.total_usage}")
            self._update_usage_from_report(
                allocation, report, update_current=is_current_month
            )

            historical_report.node_usage = report.total_usage.hours
            historical_report.is_complete = (
                not is_current_month
            ) and report.is_complete
            historical_report.save()

            # Cache the full report JSON for rich frontend consumption
            resource = str(self.client.destination())
            models.CachedProjectUsageReport.objects.update_or_create(
                year=first_day.year,
                month=first_day.month,
                project_identifier=str(project),
                resource=resource,
                defaults={
                    "is_complete": historical_report.is_complete,
                    "report": json.loads(report.to_json()),
                },
            )

    def sync_storage(self, allocation: models.Allocation):
        """
        Fetch the current storage snapshot for this allocation's project and
        merge it into the month-accumulated CachedProjectStorageReport.
        """
        if not allocation.has_project_identifier():
            logger.debug(
                f"Skipping storage sync for {allocation} - no project identifier"
            )
            return

        project = allocation.get_project_identifier()
        resource = str(self.client.destination())

        logger.debug(f"Syncing storage for {project} on {resource}")

        try:
            new_report = self.client.get_storage_report(project)
        except Exception as e:
            logger.error(f"Failed to get storage report for {project}: {e}")
            return

        # Use the report's own generated_at timestamp to determine the month,
        # avoiding a bug where a report fetched near midnight on the last day of
        # the month is processed just after midnight on the first of the next month.
        report_date = new_report.generated_at.date()
        new_report_json = json.loads(new_report.to_json())

        with transaction.atomic():
            cached, created = (
                models.CachedProjectStorageReport.objects.select_for_update().get_or_create(
                    year=report_date.year,
                    month=report_date.month,
                    project_identifier=str(project),
                    resource=resource,
                    defaults={"report": new_report_json},
                )
            )
            if created:
                logger.debug(
                    f"Created storage report for {project} [{report_date.year}-{report_date.month}]"
                )
            else:
                # Merge the new snapshot into the accumulated monthly report
                accumulated = cached.get_report()
                accumulated += new_report
                cached.report = json.loads(accumulated.to_json())
                cached.save(update_fields=["report"])
                logger.debug(
                    f"Updated storage report for {project} [{report_date.year}-{report_date.month}]"
                )

    def pull_allocation(self, allocation: models.Allocation):
        if not isinstance(allocation, models.Allocation):
            raise ServiceBackendError("Invalid allocation type %s" % type(allocation))

        if not allocation.is_added_to_openportal():
            allocation = self.add_allocated_project(allocation)

            if not allocation.is_added_to_openportal():
                logger.error(
                    f"Allocation {allocation} is not in OpenPortal - cannot pull"
                )
                return

        if not allocation.has_project_identifier():
            raise ServiceBackendError(
                "Allocation %s has no project identifier - cannot pull from OpenPortal"
                % allocation
            )

        logger.debug(f"Pulling OpenPortal allocation {allocation}")
        self.sync_users(allocation)
        self.sync_usage(allocation)

    def get_allocation_queryset(self):
        logger.debug("Getting OpenPortal allocation queryset")
        return models.Allocation.objects.filter(service_settings=self.settings)

    def _update_allocation_associations(self, allocation):
        logger.debug(f"Updating associations for allocation {allocation}")

        if not (
            allocation.has_project_identifier() or allocation.is_added_to_openportal()
        ):
            logger.error(
                f"Allocation {allocation} is not in OpenPortal - cannot update associations"
            )
            return

        project = allocation.get_project_identifier()

        # get the UserMappings for all users that are registered with
        # OpenPortal for this allocation
        backend_users = self.client.get_users(project)

        # get the UserMappings for all users that Waldur says should
        # be associated with this allocation
        local_users = [
            association.get_mapping()
            for association in allocation.associations.all()
            if association.has_mapping()
        ]

        # Get the UserIdentifiers for all users that are in OpenPortal
        # who shouldn't be (because they are not in Waldur)
        stale_users = [user.user for user in backend_users if user not in local_users]

        # Now remove the associations for all of these users
        models.Association.objects.filter(
            allocation=allocation, useridentifier__in=stale_users
        ).delete()

        logger.debug(
            "Associations for allocation %s and users %s have been removed",
            allocation,
            stale_users,
        )
