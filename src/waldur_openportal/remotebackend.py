import datetime
import decimal
import json
import logging
import re

import openportal
from django.conf import settings as django_settings
from django.core.exceptions import ObjectDoesNotExist

from waldur_core.core import utils as core_utils
from waldur_core.core.enums import CoreStates
from waldur_core.structure.backend import ServiceBackend
from waldur_core.structure.exceptions import ServiceBackendError
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_openportal import signals
from waldur_openportal.remoteclient import RemoteOpenPortalClient

from . import exceptions, models
from . import utils as openportal_utils

logger = logging.getLogger(__name__)


class RemoteOpenPortalBackend(ServiceBackend):
    def __init__(self, settings):
        self.settings = settings
        self.client = self.get_client(settings)

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
        return RemoteOpenPortalClient(
            instance_name=settings.options.get("instance_name", None),
            project_template=settings.options.get("project_template", None),
        )

    def pull_resources(self):
        logger.debug(f"Pulling OpenPortal remote resources for settings: {self}")

        logger.warning("Skipping pull_resources")
        return
        # --- IGNORE ---
        fail_count = 0
        now = datetime.datetime.now()

        from . import tasks as openportal_tasks

        for allocation in self.get_allocation_queryset().filter(
            state=CoreStates.OK, is_added=True
        ):
            if openportal_tasks.is_task_running(openportal_tasks.sync):
                logger.info(
                    "Task sync is already running - skipping allocation %s",
                    allocation,
                )
                continue

            try:
                logger.debug("About to pull allocation %s", allocation)
                self.pull_allocation(allocation)
            except Exception as e:
                logger.error("Error while pulling allocation [%s]: %s", allocation, e)
                fail_count += 1

                if fail_count > 5 and (datetime.datetime.now() - now).seconds > 60:
                    logger.error("Too many failures - aborting")
                    return
                elif (datetime.datetime.now() - now).seconds > 120:
                    logger.error("Took too long - aborting")
                    return

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

    def sync_users(self, allocation: models.RemoteAllocation) -> None:
        if not isinstance(allocation, models.RemoteAllocation):
            raise ServiceBackendError("Invalid allocation type %s" % type(allocation))

        if not (
            allocation.has_project_identifier()
            or allocation.is_added_to_openportal()
            or allocation.has_remote_project_identifier()
        ):
            logger.warning(
                f"Allocation {allocation} is not in OpenPortal - adding now!"
            )
            # this already calls 'sync_users' on the created allocation
            self.add_allocated_project(allocation)
            return

        project = allocation.get_project_identifier()
        remote_project = allocation.get_project_identifier()
        logger.debug(
            f"Syncing users for allocation: {allocation} | {project} <=> {remote_project}"
        )
        users = allocation.project.get_users()
        logger.debug(f"Users for allocation: {users}")

        # go through and add the users who are not in OpenPortal
        for user in users:
            if not user.is_active:
                logger.warning(
                    f"Removing {user} as they are no longer listed as active"
                )
                continue

            # get the association between the user and the allocation
            try:
                (association, _) = models.RemoteAssociation.objects.get_or_create(
                    user=user, allocation=allocation
                )
            except models.RemoteAssociation.MultipleObjectsReturned:
                association = openportal_utils.get_remote_association(
                    user=user, allocation=allocation
                )

            try:
                if not association.user_is_in_remote():
                    logger.info(f"Adding user {user} to OpenPortal Remote Project")

                    self.client.add_user(
                        project=project, user=user, role=association.role
                    )

                    logger.debug(
                        f"Added user {user} to OpenPortal project {project} in role {association.role}"
                    )

                    association.set_user_is_in_remote(True)
                    association.save()

                    signals.openportal_association_created.send(
                        models.RemoteAllocation,
                        allocation=allocation,
                        user=user,
                    )
            except Exception as e:
                logger.error(f"Unable to add user {user} to OpenPortal: {e}")

        # Note that we don't remove remote users - the membership is solely
        # managed by the PI on the remote system - we only add people here

    def assert_can_add_allocation(self, allocation: models.RemoteAllocation):
        """
        This checks to see if the passed allocation is allowed to be created
        on the instance managed by this backend. Projects are only allowed to create
        a single allocation per instance, and they must have a routing path
        that matches the destination of this instance.
        """
        if not isinstance(allocation, models.RemoteAllocation):
            raise ServiceBackendError("Invalid allocation type %s" % type(allocation))

        if allocation.state not in [
            CoreStates.CREATION_SCHEDULED,
            CoreStates.CREATING,
            CoreStates.UPDATE_SCHEDULED,
            CoreStates.UPDATING,
            CoreStates.OK,
        ]:
            logger.warning(
                f"Remote allocation {allocation} is not in a valid state {allocation.state} for adding - skipping"
            )
            raise ServiceBackendError(
                f"Remote allocation {allocation} is not in a valid state {allocation.state} for adding - skipping"
            )

        project = allocation.project

        if not project:
            logger.error(
                f"Allocation {allocation} does not have a project - cannot create in OpenPortal"
            )
            raise ServiceBackendError(
                f"Allocation {allocation} does not have a project - cannot create in OpenPortal"
            )

        destination = str(self.client.destination())

        logger.debug(
            f"Asserting that project {project} can create an allocation for {destination}"
        )

        existing_allocations = self.get_allocation_queryset().filter(project=project)

        # find all of these allocations that are active and that have a project identifier
        existing_allocations = [
            alloc
            for alloc in existing_allocations
            if alloc.has_project_identifier()
            and alloc.state != CoreStates.ERRED
            and alloc.has_remote_project_identifier()
            and alloc != allocation
        ]

        if len(existing_allocations) > 0:
            logger.error(
                f"Project {project} already has existing remote allocation(s) in OpenPortal for {destination}"
            )
            logger.error(f"These are {existing_allocations}")

            allocation.set_erred()
            allocation.error_message = (
                f"Project {project} already has an allocation for {destination} in OpenPortal. "
                + "You may only have a single active allocation per destination per project."
            )
            allocation.save()

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

    def _allocation_is_in_openportal(self, allocation: models.RemoteAllocation) -> bool:
        """
        Check if the allocation already exists in OpenPortal.
        This is used to avoid creating duplicate allocations.
        """
        if not isinstance(allocation, models.RemoteAllocation):
            raise ServiceBackendError("Invalid allocation type %s" % type(allocation))

        return (
            allocation.has_project_identifier()
            and allocation.is_added_to_openportal()
            and allocation.has_remote_project_identifier()
        )

    def _add_allocated_project(
        self, allocation: models.RemoteAllocation
    ) -> models.RemoteAllocation:
        if not isinstance(allocation, models.RemoteAllocation):
            raise ServiceBackendError("Invalid allocation type %s" % type(allocation))

        self.assert_can_add_allocation(allocation)

        if allocation.has_project_identifier():
            project = allocation.get_project_identifier()
            details = allocation.get_project_details()
            logger.debug(
                f"Allocation already exists: {allocation} | {project} | {details}"
            )

            # add it again just to be sure
            try:
                mapping = self.client.add_project(project, details)
            except exceptions.ManagedProjectRejectedError as e:
                logger.warning(f"OpenPortal project {project} is rejected: {e}. ")
                allocation.error_message = str(e)
                allocation.set_erred()
                allocation.save()
                return allocation
            except Exception as e:
                logger.warning(
                    f"Unable to re-add project {project} to OpenPortal: {e}. This will be re-added later..."
                )
                return allocation
        else:
            project = self.client.get_project_identifier(allocation.project)
            details = allocation.get_project_details()

            try:
                mapping = self.client.add_project(project, details)
            except exceptions.ManagedProjectRejectedError as e:
                logger.warning(f"OpenPortal project {project} is rejected: {e}. ")
                allocation.error_message = str(e)
                allocation.set_erred()
                allocation.save()
                return allocation
            except Exception as e:
                logger.warning(
                    f"Unable to create OpenPortal project for {project}: {e}. This will be created later..."
                )
                return allocation

            logger.info(f"Created OpenPortal project {project} with mapping {mapping}")
            allocation.state = CoreStates.OK
            allocation.set_mapping(mapping)
            allocation.is_added = True

        allocation.save()
        return allocation

    def add_allocated_project(self, allocation: models.RemoteAllocation):
        allocation = self._add_allocated_project(allocation)

        logger.debug(f"Allocation node limit is {allocation.node_limit}")

        if allocation.has_project_identifier():
            if (
                allocation.is_added_to_openportal()
                and allocation.has_remote_project_identifier()
            ):
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

    def update_allocated_project(
        self, allocation: models.RemoteAllocation, force_update=True
    ):
        if not isinstance(allocation, models.RemoteAllocation):
            raise ServiceBackendError("Invalid allocation type %s" % type(allocation))

        if allocation.state not in [
            CoreStates.CREATION_SCHEDULED,
            CoreStates.CREATING,
            CoreStates.UPDATE_SCHEDULED,
            CoreStates.UPDATING,
            CoreStates.OK,
        ]:
            logger.warning(
                f"Remote allocation {allocation} is not in a valid state {allocation.state} for adding - skipping"
            )
            raise ServiceBackendError(
                f"Remote allocation {allocation} is not in a valid state {allocation.state} for adding - skipping"
            )

        if not self._allocation_is_in_openportal(allocation):
            logger.warning(f"Allocation {allocation} is not in OpenPortal - re-adding")
            return self.add_allocated_project(allocation)

        project_identifier = allocation.get_project_identifier()
        project_details = allocation.get_project_details()

        if force_update:
            version = allocation.increment_version()
        else:
            version = allocation.get_version()

        if not allocation.needs_updating():
            logger.debug(
                f"Allocation {allocation} does not need updating - skipping OpenPortal update"
            )
            return

        try:
            mapping = self.client.update_project(project_identifier, project_details)
            allocation.successfully_updated(version)
            allocation.update_mapping(mapping)
            allocation.state = CoreStates.OK
            allocation.save()
        except exceptions.ManagedProjectRejectedError as e:
            logger.warning(
                f"OpenPortal project {project_identifier} is rejected: {e}. "
            )
            allocation.error_message = str(e)
            allocation.set_erred()
            allocation.save()
        except Exception as e:
            logger.warning(
                f"Unable to update OpenPortal project {project_identifier}: {e}."
            )
            allocation.state = CoreStates.UPDATING
            allocation.error_message = "Project update is still pending..."
            allocation.save()

    def check_added_allocation(
        self, allocation: models.RemoteAllocation
    ) -> models.RemoteAllocation:
        if not isinstance(allocation, models.RemoteAllocation):
            raise ServiceBackendError("Invalid allocation type %s" % type(allocation))

        if (
            allocation.has_project_identifier()
            and allocation.has_remote_project_identifier()
            and allocation.is_added_to_openportal()
        ):
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
            allocation.has_project_identifier()
            and allocation.is_added_to_openportal()
            and allocation.has_remote_project_identifier()
        ):
            logger.error(
                f"Allocation {allocation} could not be added to OpenPortal. Try again later."
            )
            raise ServiceBackendError(
                f"Allocation {allocation} could not be added to OpenPortal. Try again later."
            )

        return allocation

    def create_allocation(self, allocation):
        if not isinstance(allocation, models.RemoteAllocation):
            raise ServiceBackendError("Invalid allocation type %s" % type(allocation))

        allocation = self._add_allocated_project(allocation)

        default_limits = django_settings.WALDUR_OPENPORTAL["DEFAULT_LIMITS"]
        allocation.node_limit = decimal.Decimal(default_limits["NODE"])
        allocation.save()

        if allocation.has_project_identifier():
            if (
                allocation.is_added_to_openportal()
                and allocation.has_remote_project_identifier()
            ):
                # schedule syncing users as a background task so that we don't block the Waldur GUI
                # If this fails, then another sync process will fix things later
                from . import tasks

                tasks.sync_remote_allocation_users.delay(
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
        if not isinstance(allocation, models.RemoteAllocation):
            raise ServiceBackendError("Invalid allocation type %s" % type(allocation))

        logger.info(f"Deleting allocation: {allocation}")

        try:
            project = allocation.get_project_identifier()
        except Exception:
            project = self.client.get_project_identifier(allocation.project)

        try:
            self.client.delete_project(project)
            allocation.remote_project_identifier = None
            allocation.is_added = False
            allocation.save()
        except Exception as e:
            logger.error(
                f"Unable to delete allocation {allocation} from OpenPortal: {e}"
            )

    def add_user(self, allocation: models.RemoteAllocation, user) -> bool:
        """
        Create association between user and OpenPortal account if it does not exist yet.
        The allocation contains the information of which project the user is in.
        """
        if not isinstance(allocation, models.RemoteAllocation):
            raise ServiceBackendError("Invalid allocation type %s" % type(allocation))

        # We just do an update to the project - this syncs the users
        self.update_allocated_project(allocation, force_update=True)

        return True

    def delete_user(self, allocation: models.RemoteAllocation, user) -> bool:
        """
        Delete association between user and OpenPortal account if it exists.
        """
        if not isinstance(allocation, models.RemoteAllocation):
            raise ServiceBackendError("Invalid allocation type %s" % type(allocation))

        # We just do an update to the project - this syncs the users
        self.update_allocated_project(allocation, force_update=True)

        return True

    def set_resource_limits(self, allocation: models.RemoteAllocation):
        if not isinstance(allocation, models.RemoteAllocation):
            raise ServiceBackendError("Invalid allocation type %s" % type(allocation))

        allocation = self.check_added_allocation(allocation)

        if not (
            allocation.has_project_identifier()
            and allocation.has_remote_project_identifier()
        ):
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
            # this is an empty report with no usage
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

    def _adjust_project_credits_for_reconciliation(
        self, resource, month_date: datetime.date, usage_change: float
    ):
        """
        Adjust project credits to account for billing reconciliation changes.

        When we correct historical usage, the invoice items change but credits have
        already been deducted. This method adjusts the project credit balance to
        compensate for the billing change.

        Args:
            resource: The marketplace Resource
            month_date: The month being reconciled
            usage_change: The change in usage (positive = more usage, negative = less usage)
        """
        # Skip if no change
        if abs(usage_change) < 0.015:
            return

        project = resource.project
        customer = project.customer

        # Get the invoice for this month
        try:
            invoice = invoice_models.Invoice.objects.get(
                customer=customer, year=month_date.year, month=month_date.month
            )
        except invoice_models.Invoice.DoesNotExist:
            logger.warning(
                f"No invoice found for {customer} {month_date.year}-{month_date.month:02d} - "
                f"cannot adjust credits"
            )
            return

        # Update the invoice cache to reflect new totals
        invoice.update_cache()

        # Get project credit if it exists
        project_credit = invoice_models.ProjectCredit.objects.filter(
            project=project
        ).first()

        if not project_credit:
            logger.debug(
                f"No project credit for {project.name} - no credit adjustment needed"
            )
            return

        # Calculate the billing change
        # usage_change is in hours, we need to convert to cost
        # Get the plan component to find the unit price
        try:
            plan = resource.plan
            offering_component = marketplace_models.OfferingComponent.objects.get(
                offering=resource.offering, type="node"
            )
            plan_component = plan.components.get(component=offering_component)
            unit_price = float(plan_component.price)
        except Exception as e:
            logger.error(
                f"Could not determine unit price for {resource}: {e} - "
                f"cannot adjust credits"
            )
            return

        # Calculate the cost change
        # Positive usage_change = more usage = more cost = need to reduce credit more
        # Negative usage_change = less usage = less cost = need to increase credit (refund)
        cost_change = usage_change * unit_price

        logger.info(
            f"Reconciliation credit adjustment for {project.name}: "
            f"usage_change={usage_change:.2f} hours, "
            f"unit_price={unit_price:.4f}, "
            f"cost_change={cost_change:.2f}"
        )

        # Adjust the project credit
        # If cost increased (positive cost_change), reduce credit (subtract from value)
        # If cost decreased (negative cost_change), increase credit (add to value)
        old_credit_value = project_credit.value
        project_credit.value = project_credit.value - decimal.Decimal(cost_change)
        project_credit.save(update_fields=["value"])

        logger.info(
            f"Adjusted project credit for {project.name} from {old_credit_value:.2f} "
            f"to {project_credit.value:.2f} (change: {-cost_change:.2f})"
        )

    def _reconcile_historical_usage(
        self,
        allocation: models.RemoteAllocation,
        historical_report,
        month_date: datetime.date,
    ):
        """
        Reconcile historical usage with marketplace ComponentUsage for completed months.

        This method checks if the billing (ComponentUsage) matches the actual usage
        (HistoricalRemoteAllocation) for a completed month. If there's a discrepancy,
        it updates the ComponentUsage which triggers billing adjustments via signals,
        and adjusts project credits accordingly.

        Args:
            allocation: The RemoteAllocation being synced
            historical_report: The HistoricalRemoteAllocation for the month
            month_date: The date representing the month (should be first day of month)
        """
        # Only reconcile completed months
        if not historical_report.is_complete:
            logger.debug(
                f"Skipping reconciliation for {allocation} {month_date.year}-{month_date.month:02d} - month not complete"
            )
            return

        # Get the marketplace resource
        try:
            resource = marketplace_models.Resource.objects.get(scope=allocation)
        except ObjectDoesNotExist:
            logger.warning(
                f"No marketplace Resource found for allocation {allocation} - skipping reconciliation"
            )
            return

        # Get the offering component
        try:
            offering_component = marketplace_models.OfferingComponent.objects.get(
                offering=resource.offering, type="node"
            )
        except ObjectDoesNotExist:
            logger.warning(
                f"No offering component 'node' found for resource {resource} - skipping reconciliation"
            )
            return

        # Get the plan period
        plan_period = marketplace_models.ResourcePlanPeriod.objects.filter(
            resource=resource, end=None
        ).first()

        if not plan_period:
            logger.warning(
                f"No active ResourcePlanPeriod found for resource {resource} - skipping reconciliation"
            )
            return

        # Get the billing period
        billing_period = core_utils.month_start(month_date)
        actual_usage = float(historical_report.node_usage)

        # Don't do anything if there's no usage
        if actual_usage <= 0:
            logger.debug(
                f"No usage for {allocation} {month_date.year}-{month_date.month:02d} - skipping reconciliation"
            )
            return

        # Check if ComponentUsage already exists and compare
        existing_usage = marketplace_models.ComponentUsage.objects.filter(
            resource=resource,
            component=offering_component,
            billing_period=billing_period,
            plan_period=plan_period,
        ).first()

        if existing_usage:
            billed_usage = float(existing_usage.usage)
            usage_delta = actual_usage - billed_usage

            # Only reconcile if there's a significant difference (> 0.01 hours)
            if abs(usage_delta) < 0.015:
                logger.debug(
                    f"Usage for {allocation} {month_date.year}-{month_date.month:02d} matches billing "
                    f"(actual: {actual_usage}, billed: {billed_usage})"
                )
                return

            logger.warning(
                f"Usage discrepancy detected for {allocation} {month_date.year}-{month_date.month:02d}: "
                f"actual usage={actual_usage} hours, billed usage={billed_usage} hours, delta={usage_delta} hours"
            )
        else:
            logger.warning(
                f"ComponentUsage missing for {allocation} {month_date.year}-{month_date.month:02d} "
                f"with {actual_usage} hours - will create it"
            )

        # Get or create the ComponentUsage
        component_usage, created = (
            marketplace_models.ComponentUsage.objects.get_or_create(
                resource=resource,
                component=offering_component,
                billing_period=billing_period,
                plan_period=plan_period,
                defaults={
                    "usage": actual_usage,
                    "date": datetime.datetime(
                        month_date.year,
                        month_date.month,
                        15,
                        12,
                        0,
                        0,
                        tzinfo=datetime.UTC,
                    ),
                },
            )
        )

        if created:
            logger.info(
                f"Created ComponentUsage for {allocation} {month_date.year}-{month_date.month:02d} "
                f"with {actual_usage} hours"
            )
            # New usage created - need to adjust credits by the full amount
            usage_change = actual_usage
        else:
            # Update the usage - schedule_component_usage_billing enqueues
            # process_component_usage_billing on commit; billing is async.
            old_usage = float(component_usage.usage)
            component_usage.usage = actual_usage
            component_usage.date = datetime.datetime(
                month_date.year,
                month_date.month,
                15,
                12,
                0,
                0,
                tzinfo=datetime.UTC,
            )
            component_usage.save()
            logger.info(
                f"Updated ComponentUsage for {allocation} {month_date.year}-{month_date.month:02d} "
                f"from {old_usage} to {actual_usage} hours"
            )
            # Calculate the change in usage
            usage_change = actual_usage - old_usage

        # Adjust project credits to reflect the billing change
        self._adjust_project_credits_for_reconciliation(
            resource, month_date, usage_change
        )

    def sync_usage(self, allocation: models.RemoteAllocation):
        if not isinstance(allocation, models.RemoteAllocation):
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
                models.HistoricalRemoteAllocation.objects.get_or_create(
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
            else:
                report = self.client.get_usage_report(project, month)

                if report.total_usage.seconds > 0:
                    self._update_usage_from_report(
                        allocation, report, update_current=is_current_month
                    )

                historical_report.node_usage = report.total_usage.hours
                historical_report.is_complete = (
                    not is_current_month
                ) and report.is_complete
                historical_report.save()

                # Cache the full report JSON so the remote portal can serve
                # rich usage data (per-user, per-date) without re-fetching
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

            # Reconcile historical usage with billing for completed months
            try:
                self._reconcile_historical_usage(
                    allocation, historical_report, first_day
                )
            except Exception as e:
                logger.error(
                    f"Failed to reconcile historical usage for {allocation} in {month}: {e}"
                )

    def sync_storage(self, allocation: models.RemoteAllocation):
        """
        Fetch the accumulated storage report from the remote portal for this
        allocation and store it in CachedProjectStorageReport.  Last month and
        the current month are always fetched so that data is current across
        month boundaries.
        """
        if not isinstance(allocation, models.RemoteAllocation):
            raise ServiceBackendError("Invalid allocation type %s" % type(allocation))

        if not allocation.is_added_to_openportal():
            allocation = self.add_allocated_project(allocation)

            if not allocation.is_added_to_openportal():
                logger.error(
                    f"Allocation {allocation} is not in OpenPortal"
                    " - cannot sync storage"
                )
                return

        if not allocation.has_project_identifier():
            logger.error(
                f"Allocation {allocation} has no project identifier"
                " - cannot sync storage"
            )
            return

        project = allocation.get_project_identifier()
        logger.debug(
            f"Syncing OpenPortal storage for allocation {allocation}"
            f" and project {project}"
        )

        months = [
            openportal.DateRange.last_month(),
            openportal.DateRange.this_month(),
        ]

        resource = str(self.client.destination())

        for month in months:
            first_day = month.days[0]

            try:
                report = self.client.get_storage_report(project, month)
            except exceptions.OpenPortalError as e:
                logger.warning(
                    f"Failed to get storage report for {allocation} in {month}: {e}"
                )
                continue
            except Exception as e:
                logger.error(
                    f"Failed to get storage report for {allocation} in {month}: {e}"
                )
                continue

            report_json = json.loads(report.to_json())

            models.CachedProjectStorageReport.objects.update_or_create(
                year=first_day.year,
                month=first_day.month,
                project_identifier=str(project),
                resource=resource,
                defaults={"report": report_json},
            )
            logger.debug(
                f"Stored storage report for {project}"
                f" [{first_day.year}-{first_day.month:02d}]"
            )

    def pull_allocation(self, allocation):
        logger.info(f"Pulling remote allocation: {allocation}")

    def get_allocation_queryset(self):
        logger.debug("Getting OpenPortal allocation queryset")
        return models.RemoteAllocation.objects.filter(service_settings=self.settings)

    def _update_allocation_associations(self, allocation):
        logger.debug(f"Updating associations for allocation {allocation}")
