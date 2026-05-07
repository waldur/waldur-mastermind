import decimal
import json
import logging
from datetime import date, timedelta

import openportal
from django.utils import timezone

from waldur_core.core.enums import ReviewStates
from waldur_core.structure import models as structure_models
from waldur_mastermind.invoices import models as invoice_models

from . import config, exceptions, models, utils

logger = logging.getLogger(__name__)


PROJECT_GRACE_PERIOD_DAYS = 30


class OpenPortalBoard:
    """
    This class implements the OpenPortal job board interface,
    letting you get jobs, process them, and send back results.

    See also: https://github.com/isambard-sc/openportal
    """

    def __init__(self, destination: openportal.Destination = None):
        # make sure that the OpenPortal config is loaded
        if not config.ensure_config_loaded():
            raise exceptions.OpenPortalError(
                "OpenPortal is not enabled or configuration is not available"
            )

        if destination is not None:
            if not isinstance(destination, openportal.Destination):
                destination = openportal.Destination(destination)

        self._destination = destination

    def _to_project_identifier(self, project) -> openportal.ProjectIdentifier:
        """
        Convert the passed (any) object into a ProjectIdentifier
        """
        if not isinstance(project, openportal.ProjectIdentifier):
            try:
                project = openportal.ProjectIdentifier(project)
            except Exception:
                project = openportal.ProjectIdentifier(f"{project}.{self.portal()}")

        return project

    def _to_user_identifier(self, user) -> openportal.UserIdentifier:
        """
        Convert the passed (any) object into a UserIdentifier
        """
        if not isinstance(user, openportal.UserIdentifier):
            try:
                user = openportal.UserIdentifier(user)
            except Exception:
                user = openportal.UserIdentifier(f"{user}.{self.portal()}")

        return user

    def portal(self) -> openportal.PortalIdentifier:
        """
        Return the name of the portal that represents the web portal
        connected to by the Bridge managed by this board.
        """
        try:
            return openportal.PortalIdentifier(self._destination.agents[0])
        except Exception as e:
            logger.error(
                f"Failed to get portal name from destination {self._destination}: {e}"
            )
            raise exceptions.OpenPortalError(
                f"Failed to get portal name from destination {self._destination}: {e}"
            )

    def destination(self) -> openportal.Destination:
        """
        Return the destination that this board is connected to.
        This is the destination that the OpenPortal Bridge is connected to.
        """
        if self._destination is None:
            raise exceptions.OpenPortalError("Board is not connected to a destination")

        return self._destination

    def offering(self) -> str:
        """
        Return the offering connected to this board. This is the final agent
        name in the destination
        """
        if self._destination is None:
            raise exceptions.OpenPortalError("Board is not connected to a destination")

        return str(self._destination.agents[-1])

    def load_config(self):
        """
        Load the OpenPortal configuration from the file specified
        in the OPENPORTAL_CONFIG environment variable. Raises an
        OpenPortalException if the environment variable is not set
        or if the config file cannot be loaded
        """
        config.ensure_config_loaded()

    def health(self):
        if not config.is_config_available():
            raise exceptions.OpenPortalError(
                "OpenPortal is not enabled or configuration is not available"
            )

        try:
            health = openportal.health()
        except Exception as e:
            raise exceptions.OpenPortalError(f"Failed to get OpenPortal health: {e}")

        if not health.is_healthy():
            logger.error(f"OpenPortal is not healthy: {health}")
            raise exceptions.OpenPortalError(f"OpenPortal is not healthy: {health}")

    def fetch_job(self, job_id: str) -> openportal.Job:
        """
        Fetch the OpenPortal job with the specified job_id
        """
        if not config.is_config_available():
            raise exceptions.OpenPortalError(
                f"OpenPortal is not enabled or configuration is not available - cannot fetch job with ID '{job_id}'"
            )

        try:
            job = openportal.fetch_job(str(job_id))
        except Exception as e:
            raise exceptions.OpenPortalError(
                f"Failed to fetch job with ID '{job_id}': {e}"
            )

        return job

    def _get_project_template(
        self, managed_project: models.ManagedProject, details: openportal.AwardDetails
    ) -> models.ProjectTemplate:
        """
        Get the project class for the managed project.

        Note that this will delete the ManagedProject if the project class
        is invalid and cannot be determined. This is because an invalid
        project class means that this project cannot be created
        """
        if managed_project.has_project_template():
            managed_project.set_details(details)
            managed_project.save()
            return managed_project.get_project_template()

        if not managed_project.has_remote_identifier():
            managed_project.delete()

            raise exceptions.ManagedProjectRejectedError(
                f"ManagedProject {managed_project} does not have an identifier set"
            )

        identifier = managed_project.get_remote_identifier()

        # get the project class of the new project
        if details.project_template is None:
            managed_project.delete()

            raise exceptions.ManagedProjectRejectedError(
                f"Project template is not set for project {details}"
            )

        if not isinstance(details.project_template, openportal.ProjectTemplate):
            managed_project.delete()

            raise exceptions.ManagedProjectRejectedError(
                f"Invalid project class: {details.project_template}"
            )

        project_template = str(details.project_template).strip()

        if len(project_template) == 0:
            managed_project.delete()

            raise exceptions.ManagedProjectRejectedError(
                f"Project class is empty for project {managed_project}"
            )

        # Make sure that we are the right board to manage this project
        project_destination = managed_project.get_destination()

        if project_destination != self.destination():
            logger.error(
                f"ManagedProject {managed_project} is not managed by this board. "
                f"Expected destination {self.destination()}, got {project_destination}."
            )
            managed_project.delete()

            raise exceptions.ManagedProjectRejectedError(
                f"ManagedProject {managed_project} is not managed by this board. "
                f"Expected destination {self.destination()}, got {project_destination}."
            )

        # See if we have an existing ProjectTemplate for the requesting remote portal and class
        remote_portal = str(identifier.portal)

        try:
            project_template = models.ProjectTemplate.objects.filter(
                portal=remote_portal, name=project_template, offering=self.offering()
            ).first()
        except Exception:
            managed_project.delete()

            logger.warning(
                f"Failed to get the project template for portal {remote_portal} for {project_template}@{self.offering()}. "
                "This suggests that the portal is not allowed to create projects in this template for this offering."
            )
            raise exceptions.ManagedProjectRejectedError(
                f"{project_template}@{self.offering()} is not allowed for portal '{remote_portal}'"
            )

        if not project_template:
            managed_project.delete()

            logger.warning(
                f"Failed to get the project template for portal {remote_portal} for {details.project_template}@{self.offering()}. "
                "This suggests that the portal is not allowed to create projects in this template for this offering."
            )
            raise exceptions.ManagedProjectRejectedError(
                f"{details.project_template}@{self.offering()} is not allowed for portal '{remote_portal}'"
            )

        # now check the key, if one is set in ProjectTemplate
        try:
            project_template.assert_matching_key(details.key)
        except Exception:
            managed_project.delete()

            logger.warning(
                f"Failed to validate key for portal {remote_portal} for {details.project_template}@{self.offering()}. "
                "This suggests that the portal was not allowed to create projects in this template for this offering."
            )
            raise exceptions.ManagedProjectRejectedError(
                f"{details.project_template}@{self.offering()} is not allowed for portal '{remote_portal}'"
            )

        # The remote portal is allowed to create projects in this class,
        # so we can now safely save the ManagedProject and create the project
        managed_project.set_project_template(project_template)
        managed_project.set_details(details)
        managed_project.save()

        return project_template

    def _attach_existing_project(
        self,
        managed_project: models.ManagedProject,
        existing_project: structure_models.Project,
    ) -> openportal.ProjectMapping:
        if managed_project.project is None:
            managed_project.project = existing_project
            managed_project.save()
        elif managed_project.project != existing_project:
            # This is a bug - we should not have a ManagedProject with a different project
            logger.error(
                f"ManagedProject {managed_project} already has a project {managed_project.project}, but we are trying to attach {existing_project}"
            )
            raise exceptions.OpenPortalError(
                f"ManagedProject {managed_project} already has a project {managed_project.project}, but we are trying to attach {existing_project}"
            )

        identifier = managed_project.get_remote_identifier()

        if identifier is None:
            # This is a bug - we should not have a ManagedProject without an identifier
            logger.error(f"{managed_project} does not have an identifier set.")
            managed_project.reject(
                utils.get_openportal_robot(),
                f"{managed_project} does not have an identifier set.",
            )
            raise exceptions.ManagedProjectRejectedError()

        if managed_project.project.is_expired or managed_project.project.is_removed:
            # any changes to this project are now not allowed - this is rejected
            logger.warning(
                f"{identifier} is expired or removed, cannot create project."
            )
            managed_project.reject(
                utils.get_openportal_robot(),
                f"{identifier} is expired or removed, cannot create project.",
            )
            raise exceptions.ManagedProjectRejectedError()

        # we have already created this project, so we can just return the mapping - check
        # there the project details are in agreement with the existing project
        if managed_project.details is None:
            # This is a bug
            logger.warning(
                f"Details for {identifier} are None, but the project already exists."
            )
            managed_project.reject(
                utils.get_openportal_robot(),
                f"{identifier} details are None, but the project already exists.",
            )
            raise exceptions.ManagedProjectRejectedError()

        if managed_project.local_identifier is None:
            self._get_local_identifier(managed_project)

        return managed_project.get_mapping()

    def _get_local_identifier(self, managed_project: models.ManagedProject):
        # Now create a unique shortname for this project using
        # the generator from the project class
        if managed_project is None:
            raise exceptions.ManagedProjectRejectedError(
                f"ManagedProject {managed_project} is None - cannot generate local identifier"
            )

        waldur_project = managed_project.project
        project_template = managed_project.get_project_template()

        if waldur_project is None:
            logger.error(
                f"ManagedProject {managed_project} does not have a project set."
            )
            managed_project.reject(
                utils.get_openportal_robot(),
                f"ManagedProject {managed_project} does not have a project set.",
            )
            raise exceptions.ManagedProjectRejectedError()

        project_info, created = models.ProjectInfo.objects.get_or_create(
            project=waldur_project,
        )

        if project_info.has_shortname():
            shortname = project_info.get_shortname()
        else:
            project_template = managed_project.get_project_template()

            if project_template is None:
                logger.error(
                    f"Project class is not set for project {managed_project.project}"
                )
                managed_project.reject(
                    utils.get_openportal_robot(),
                    f"Project class is not set for project {managed_project.project}",
                )
                raise exceptions.ManagedProjectRejectedError()

            generator = project_template.get_generator()

            if generator is None:
                logger.error(
                    f"Project class {project_template} does not have a generator set."
                )
                managed_project.reject(
                    utils.get_openportal_robot(),
                    f"Project class {project_template} does not have a generator set.",
                )
                raise exceptions.ManagedProjectRejectedError()

            shortname = project_info.generate_shortname(generator)

        managed_project.local_identifier = str(self._to_project_identifier(shortname))
        managed_project.save()

    def _link_existing_project(self, managed_project: models.ManagedProject):
        if managed_project.project is not None:
            # This project already exists - nothing to do?
            return

        identifier = managed_project.get_remote_identifier()
        project_template: models.ProjectTemplate = managed_project.project_template
        details: openportal.AwardDetails = managed_project.get_details()

        if identifier is None or project_template is None or details is None:
            # This is a bug - we should not have a ManagedProject without a project class
            logger.error(f"{managed_project} is in an invalid state.")
            managed_project.reject(
                utils.get_openportal_robot(),
                f"{managed_project} is in an invalid state - project class or customer is not set.",
            )
            raise exceptions.ManagedProjectRejectedError(
                f"{managed_project} is in an invalid state - project class or customer is not set."
            )

        if project_template.customer is None:
            managed_project.reject(
                utils.get_openportal_robot(),
                f"Project class {project_template} does not have a customer set.",
            )
            logger.warning(
                f"Project class {project_template} does not have a customer set."
            )
            raise exceptions.ManagedProjectRejectedError(
                f"Project class {project_template} does not have a customer set."
            )

        customer = project_template.customer

        existing_projects = structure_models.Project.objects.filter(
            customer=customer,
            name=str(details.name).strip(),
        )

        orphaned_existing_project = None

        for existing_project in existing_projects:
            # check if this project already has a ManagedProject for any destination
            try:
                existing_managed_project = models.ManagedProject.objects.get(
                    project=existing_project,
                )
                logger.info(
                    f"Found existing ManagedProject {existing_managed_project} for project {existing_project}"
                )
            except models.ManagedProject.DoesNotExist:
                logger.info(
                    f"Found existing project {existing_project} without a ManagedProject"
                )

                if not (existing_project.is_expired or existing_project.is_removed):
                    # this project is not expired or removed, so we can use it
                    # as an orphaned project
                    logger.info(
                        f"Using existing project {existing_project} for identifier {identifier}"
                    )
                    orphaned_existing_project = existing_project
                    break

        if orphaned_existing_project:
            # We have found an existing project that does not have a ManagedProject
            # associated with it. This means that the project was created in the
            # customer, but not managed by OpenPortal.
            logger.info(
                f"Using orphaned existing project {orphaned_existing_project} for identifier {identifier}"
            )

            # We can now attach this project to the ManagedProject
            self._attach_existing_project(managed_project, orphaned_existing_project)

            if managed_project.is_rejected():
                raise exceptions.ManagedProjectRejectedError()
            else:
                # We need to ask the site admin to approve this connection
                managed_project.set_needs_approval(
                    True,
                    comment=f"Project '{orphaned_existing_project}' already exists in customer '{customer}' and is being attached to '{managed_project}'.",
                )
                raise exceptions.ManagedProjectPendingError()

    def _create_local_project(self, managed_project: models.ManagedProject):
        if managed_project.project is not None:
            # This project already exists - nothing to do?
            return

        today = date.today()
        end_date = managed_project.get_details().end_date

        if end_date is not None and end_date <= today:
            raise exceptions.ManagedProjectRejectedError(
                f"End date {end_date} is today or in the past - cannot create a new project!"
            )

        identifier = managed_project.get_remote_identifier()
        project_template: models.ProjectTemplate = managed_project.project_template
        details: openportal.AwardDetails = managed_project.get_details()

        if identifier is None or project_template is None or details is None:
            # This is a bug - we should not have a ManagedProject without a project class
            logger.error(f"{managed_project} is in an invalid state.")
            managed_project.reject(
                utils.get_openportal_robot(),
                f"{managed_project} is in an invalid state - project class or customer is not set.",
            )
            raise exceptions.ManagedProjectRejectedError()

        # get the customer (organisation) in which the project should be created
        if project_template.customer is None:
            managed_project.reject(
                utils.get_openportal_robot(),
                f"Project class {project_template} does not have a customer set.",
            )
            logger.warning(
                f"Project class {project_template} does not have a customer set."
            )
            raise exceptions.ManagedProjectRejectedError()

        customer = project_template.customer

        # make sure to try to link an existing project first
        self._link_existing_project(managed_project)

        # now get a generator for the project shortname
        generator = project_template.get_generator()

        if not generator:
            managed_project.reject(
                utils.get_openportal_robot(),
                f"Project class {project_template} does not have a generator.",
            )
            logger.warning(
                f"Project class {project_template} does not have a generator."
            )
            raise exceptions.ManagedProjectRejectedError()

        # at a minimum, we need to know the name of the project
        if details.name is None:
            managed_project.reject(
                utils.get_openportal_robot(),
                f"Project name is not set for project {details}",
            )
            logger.warning(f"Project name is not set for project {details}")
            raise exceptions.ManagedProjectRejectedError()

        project_name = str(details.name).strip()

        if len(project_name) == 0:
            managed_project.reject(
                utils.get_openportal_robot(),
                f"Project name is empty for project {identifier}",
            )
            logger.warning(f"Project name is empty for project {identifier}")
            raise exceptions.ManagedProjectRejectedError()

        # create the project in the customer
        waldur_project = structure_models.Project.objects.create(
            name=project_name,
            customer=customer,
        )

        managed_project.project = waldur_project
        managed_project.save()

        self._get_local_identifier(managed_project)

    def create_project(
        self,
        identifier: openportal.ProjectIdentifier,
        details: openportal.AwardDetails,
        force_request_approval: bool = False,
    ) -> openportal.ProjectMapping:
        """
        Create a project in OpenPortal with the given identifier and details.
        This returns the mapping from the identifier in the requesting portal
        to the OpenPortal project identifier used internally.
        """
        logger.info(f"Creating project {identifier} with details {details}")

        if not isinstance(identifier, openportal.ProjectIdentifier):
            raise exceptions.ManagedProjectRejectedError(
                f"Invalid project identifier: {identifier}"
            )

        if not isinstance(details, openportal.AwardDetails):
            raise exceptions.ManagedProjectRejectedError(
                f"Invalid project details: {details}"
            )

        if self.destination() is None:
            raise exceptions.ManagedProjectRejectedError(
                "Board is not connected to a destination"
            )

        today = date.today()

        if (
            details.end_date is not None
            and details.end_date + timedelta(days=PROJECT_GRACE_PERIOD_DAYS) < today
        ):
            raise exceptions.ManagedProjectRejectedError(
                f"End date {details.end_date} is in the past"
            )

        # Get (or create) the ManagedProject for the given project identifier
        managed_project, created = models.ManagedProject.objects.get_or_create(
            destination=str(self.destination()),
            identifier=str(identifier),
            defaults={
                "details": json.loads(str(details)),
                "local_identifier": None,
                "project_template": None,
                "project": None,
                "state": ReviewStates.DRAFT,
                "reviewed_by": None,
                "reviewed_at": None,
                "review_comment": None,
            },
        )

        if created:
            logger.info(
                f"Created new ManagedProject for identifier {identifier} in {self.destination()}: {managed_project}"
            )
        else:
            logger.info(
                f"Retrieved existing ManagedProject for identifier {identifier} in {self.destination()}: {managed_project}"
            )

        # get the project class of this project
        project_template = self._get_project_template(managed_project, details)

        if project_template is None:
            # This is a bug - we should not have a ManagedProject without a project class
            logger.error(f"{identifier} does not have a project class set")
            managed_project.delete()

            raise exceptions.ManagedProjectRejectedError(
                f"{identifier} does not have a project class set"
            )

        if details.allocation is not None:
            credits = decimal.Decimal(
                project_template.convert_to_credits(details.allocation)
            )

            if project_template.action_is_rejected(allocation=float(credits)):
                logger.info(
                    f"{identifier} with class {project_template} is rejected as the allocation exceeds the limit."
                )

                # Save the merged details so can debug
                details = managed_project.get_details().merge(details)
                managed_project.set_details(details)

                managed_project.reject(
                    utils.get_openportal_robot(),
                    f"{identifier} is rejected as allocation exceeds the limit.",
                )
                raise exceptions.ManagedProjectRejectedError()

        # Try to link an existing project first
        self._link_existing_project(managed_project)

        if force_request_approval:
            if not managed_project.is_rejected():
                # If the project is not rejected, we need to set it to needs approval
                logger.info(
                    f"Project {identifier} requires approval for creation due to force_request_approval"
                )
                managed_project.set_needs_approval()

        # We can't do anything if the project is pending approval or canceled
        if managed_project.is_pending():
            logger.warning(f"{identifier} is pending approval!")
            raise exceptions.ManagedProjectPendingError()
        elif managed_project.is_canceled():
            logger.warning(f"{identifier} is canceled!")
            raise exceptions.ManagedProjectRejectedError("The project is canceled.")
        elif managed_project.is_rejected():
            logger.warning(f"{identifier} is rejected!")
            raise exceptions.ManagedProjectRejectedError()

        if (
            project_template.action_needs_approval()
            and not managed_project.is_approved()
        ):
            # We need to approve creation requests for this project class
            logger.info(
                f"Project {identifier} with class {managed_project.project_template} requires approval for project creation"
            )
            managed_project.set_needs_approval()

            # Here you would typically send a notification to the admin or
            # the person responsible for approving project creation requests.
            # For now, we will just raise an error to indicate that approval is needed.
            raise exceptions.ManagedProjectPendingError()
        elif not managed_project.is_approved():
            # If the project class does not require approval, we can proceed
            logger.info(
                f"Project {identifier} with class {managed_project.project_template} does not require approval for project creation."
            )
            managed_project.set_needs_approval(False)

        if managed_project.project is not None:
            return self._attach_existing_project(
                managed_project, managed_project.project
            )

        self._create_local_project(managed_project)

        # now force an update of the project details
        return self.update_project(
            identifier=identifier,
            new_details=details,
        )

    def update_project(
        self,
        identifier: openportal.ProjectIdentifier,
        new_details: openportal.AwardDetails,
        force_approve: bool = False,
    ) -> openportal.ProjectMapping:
        """
        Update a project in OpenPortal with the given identifier and details.
        This returns the mapping from the identifier in the requesting portal
        to the OpenPortal project identifier used internally.
        """
        logger.info(f"Updating project {identifier} with details {new_details}")

        today = date.today()

        if (
            new_details.end_date is not None
            and new_details.end_date + timedelta(days=PROJECT_GRACE_PERIOD_DAYS) < today
        ):
            raise exceptions.ManagedProjectRejectedError(
                f"End date {new_details.end_date} is in the past"
            )

        if not isinstance(identifier, openportal.ProjectIdentifier):
            raise exceptions.ManagedProjectRejectedError(
                f"Invalid project identifier: {identifier}"
            )

        if not isinstance(new_details, openportal.AwardDetails):
            raise exceptions.ManagedProjectRejectedError(
                f"Invalid project details: {new_details}"
            )

        # Get the ManagedProject for this identifier, which must already exist
        try:
            managed_project = models.ManagedProject.objects.get(
                identifier=str(identifier),
                destination=str(self.destination()),
            )
        except models.ManagedProject.DoesNotExist:
            logger.warning(
                f"ManagedProject for identifier {identifier} and destination {self.destination()} does not exist - recreating."
            )

            # recreate the project, but make sure to ask for approval
            # so that the site admin can reject this request
            return self.create_project(
                identifier=identifier, details=new_details, force_request_approval=True
            )

        project_template = managed_project.get_project_template()

        if project_template is None:
            # This is a bug - we should not have a ManagedProject without a project class
            logger.error(
                f"{identifier} does not have a project class set. Cannot update project."
            )
            managed_project.delete()
            raise exceptions.ManagedProjectRejectedError(
                f"{identifier} does not have a project class set"
            )

        if new_details.allocation is not None:
            credits = decimal.Decimal(
                project_template.convert_to_credits(new_details.allocation)
            )

            if project_template.action_is_rejected(allocation=float(credits)):
                logger.info(
                    f"{identifier} with class {project_template} is rejected as the allocation exceeds the limit."
                )

                # Save the merged details so can debug
                new_details = managed_project.get_details().merge(new_details)
                managed_project.set_details(new_details)

                managed_project.reject(
                    utils.get_openportal_robot(),
                    f"{identifier} is rejected as allocation exceeds the limit.",
                )
                raise exceptions.ManagedProjectRejectedError()

        if (
            project_template.action_needs_approval()
            and not managed_project.is_approved()
        ):
            logger.info(
                f"{identifier} with class {project_template} requires approval for project updates."
            )

            # Make sure to save the updated request
            managed_project.set_details(
                managed_project.get_details().merge(new_details)
            )

            managed_project.set_needs_approval()

            raise exceptions.ManagedProjectPendingError()

        # We can't do anything if the project is pending approval or canceled
        if managed_project.is_pending():
            logger.warning(f"{identifier} is pending approval!")

            # We should update the project details to reflect the pending state
            managed_project.set_details(
                managed_project.get_details().merge(new_details)
            )

            raise exceptions.ManagedProjectPendingError()
        elif managed_project.is_canceled():
            logger.warning(f"{identifier} is canceled!")
            raise exceptions.ManagedProjectRejectedError("The project is canceled.")
        elif managed_project.is_rejected():
            logger.warning(f"{identifier} is rejected!")
            raise exceptions.ManagedProjectRejectedError()

        if managed_project.project is None:
            # we actually need to create the project
            logger.warning(
                f"ManagedProject {managed_project} does not have an associated project, creating a new one."
            )
            self._create_local_project(managed_project)

        # Always reject updates to removed projects
        if managed_project.project.is_removed:
            managed_project.reject(
                utils.get_openportal_robot(),
                f"{identifier} is removed, cannot update project.",
            )
            logger.warning(f"{identifier} is removed, cannot update project.")
            raise exceptions.ManagedProjectRejectedError()

        # Check if trying to reactivate with a future end_date
        is_reactivating = (
            new_details.end_date is not None
            and new_details.end_date >= timezone.now().date()
        )

        # Reject updates for expired projects unless still in grace period or reactivating
        if (
            managed_project.project.is_expired
            and not managed_project.project.is_in_grace_period
            and not is_reactivating
        ):
            managed_project.reject(
                utils.get_openportal_robot(),
                f"{identifier} is expired, cannot update project.",
            )
            logger.warning(f"{identifier} is expired, cannot update project.")
            raise exceptions.ManagedProjectRejectedError()

        if managed_project.local_identifier is None:
            logger.warning(
                f"ManagedProject {managed_project} does not have a local identifier, copying from the Project."
            )
            self._get_local_identifier(managed_project)

        # get the project being managed
        project = managed_project.project

        # merge in the new details
        logger.info(f"Merging new details into project {identifier}: {new_details}")
        details = managed_project.get_details().merge(new_details)
        logger.info(f"New details after merge: {details}")
        managed_project.set_details(details)
        logger.info(f"Updated ManagedProject {managed_project} with new details.")

        # We still go through and check everything, in case the
        # project has moved away from the requested details
        # (or previous syncs failed to complete)
        update_fields = []

        logger.info("Updating project details...")

        if details.name is not None:
            if details.name != project.name:
                logger.info(
                    f"Updating project name from {project.name} to {details.name}"
                )
                project.name = details.name
                update_fields.append("name")

        if details.description is not None:
            if details.description != project.description:
                logger.info(
                    f"Updating project description from {project.description} to {details.description}"
                )
                project.description = details.description
                update_fields.append("description")

        if details.start_date is not None:
            if details.start_date != project.start_date:
                logger.info(
                    f"Updating project start date from {project.start_date} to {details.start_date}"
                )
                project.start_date = new_details.start_date
                update_fields.append("start_date")

        if details.end_date is not None:
            if details.end_date != project.end_date:
                logger.info(
                    f"Updating project end date from {project.end_date} to {details.end_date}"
                )
                project.end_date = details.end_date
                update_fields.append("end_date")

        if len(update_fields) > 0:
            logger.info(
                f"Updating project {identifier} with fields: {', '.join(update_fields)}"
            )
            project.save(update_fields=update_fields)

        if (
            project.is_expired and not project.is_in_grace_period
        ) or project.is_removed:
            # we can't make any further changes to this project - return an error
            managed_project.reject(
                utils.get_openportal_robot(),
                f"{identifier} is expired or removed, cannot update project.",
            )
            logger.warning(
                f"{identifier} is expired or removed, cannot update project."
            )
            raise exceptions.ManagedProjectRejectedError()

        if details.allocation is not None:
            new_credits = decimal.Decimal(
                project_template.convert_to_credits(details.allocation)
            )
            current_credits = utils.get_project_credits(project)

            if new_credits < decimal.Decimal(0.0):
                new_credits = decimal.Decimal(0.0)

            if abs(new_credits - current_credits) > decimal.Decimal(0.0):
                logger.info(
                    f"Allocation for project {identifier} has changed from {current_credits} to {new_credits}"
                )

                # check that we approve this allocation change
                if project_template.action_is_rejected(allocation=float(new_credits)):
                    logger.info(
                        f"{identifier} with class {project_template} is rejected as the allocation exceeds the limit."
                    )
                    managed_project.reject(
                        utils.get_openportal_robot(),
                        f"{identifier} is rejected as allocation exceeds the limit.",
                    )
                    raise exceptions.ManagedProjectRejectedError()

                # Only check credits if we are increasing the allocation
                elif (
                    new_credits - current_credits > decimal.Decimal(0.0)
                    and project_template.action_needs_approval(
                        allocation=float(new_credits)
                    )
                    and not force_approve
                ):
                    logger.info(
                        f"{identifier} with class {project_template} requires approval for allocation changes."
                    )
                    managed_project.set_needs_approval()
                    raise exceptions.ManagedProjectPendingError()

                logger.info(
                    f"Setting allocation {details.allocation} for project {identifier}"
                )
                utils.set_project_credits(project, new_credits)

                details.allocation = new_details.allocation

        # Updating membership last, as we need to know the project is ok
        if details.members is not None:
            # Update the members of the project
            current_members = utils.get_project_members(project)

            # Go through the new members and either change their role
            # if they exist, or send an invitation to join the project
            for email, role in details.members.items():
                # Get the matching role from the project class
                try:
                    role = project_template.get_local_role_for(role)
                except Exception:
                    logger.warning(
                        f"No matching role found for {role} in {project_template}."
                    )
                    role = None

                role_name = role.name if role else None

                # Get the existing role for this user if they are already a member
                existing_role_name = current_members.get(email, None)

                if existing_role_name == role_name:
                    # nothing to do
                    continue

                if role is not None:
                    # always send an email invitation to the user so
                    # that they have to actively accept the role in the
                    # project
                    utils.invite_user_to_project(
                        project=project,
                        email=email,
                        role=role,
                        send_email=True,
                    )

        return managed_project.get_mapping()

    def remove_project(
        self, identifier: openportal.ProjectIdentifier
    ) -> openportal.ProjectMapping:
        """
        Remove a project in OpenPortal with the given identifier.
        This will delete the ManagedProject, but will not delete
        the project itself - this just severs the link between
        the remote portal and the site portal.
        """
        logger.info(f"Removing project {identifier}")

        if not isinstance(identifier, openportal.ProjectIdentifier):
            raise openportal.OpenPortalError(
                f"Invalid project identifier: {identifier}"
            )

        # Get the ManagedProject for this identifier, which must already exist
        try:
            managed_project = models.ManagedProject.objects.get(
                identifier=str(identifier),
                destination=str(self.destination()),
            )
        except models.ManagedProject.DoesNotExist:
            logger.error(f"ManagedProject for identifier {identifier} does not exist.")
            raise openportal.OpenPortalError(
                f"ManagedProject for identifier '{identifier}' does not exist"
            )

        # Delete the ManagedProject
        managed_project.delete()

        # If the project was deleted, we can return None as there is no mapping anymore
        return openportal.ProjectMapping(f"{identifier}:None")

    def get_project(
        self, identifier: openportal.ProjectIdentifier
    ) -> openportal.AwardDetails:
        """
        Get a project from OpenPortal with the given identifier.
        This returns the details of the project, e.g. its name,
        description, members etc.
        """
        if not isinstance(identifier, openportal.ProjectIdentifier):
            raise openportal.OpenPortalError(
                f"Invalid project identifier: {identifier}"
            )

        # Get the ManagedProject for this identifier, which must already exist
        try:
            managed_project = models.ManagedProject.objects.get(
                identifier=str(identifier),
                destination=str(self.destination()),
            )
        except models.ManagedProject.DoesNotExist:
            logger.error(f"ManagedProject for identifier {identifier} does not exist.")
            raise openportal.OpenPortalError(
                f"ManagedProject for identifier '{identifier}' does not exist"
            )

        if managed_project.project is None:
            logger.error(
                f"ManagedProject {managed_project} does not have an associated project."
            )
            raise openportal.OpenPortalError(
                f"ManagedProject '{managed_project}' does not have an associated project"
            )

        project = managed_project.project

        if project.is_expired or project.is_removed:
            # we can't make any changes to this project - return an error
            logger.error(f"ManagedProject {managed_project} is expired or removed.")
            raise openportal.OpenPortalError(
                f"ManagedProject '{managed_project}' is expired or removed"
            )

        details = openportal.AwardDetails("{}")

        if project.name is not None:
            details.name = str(project.name).strip()

        if project.description is not None:
            details.description = str(project.description).strip()

        if managed_project.project_template is not None:
            details.project_template = openportal.ProjectTemplate(
                managed_project.project_template.name,
            )

        # Eventually add in the users in their roles etc.

        return details

    def get_projects(
        self, portal: openportal.PortalIdentifier
    ) -> list[openportal.ProjectMapping]:
        """
        Get all projects in OpenPortal for the given portal identifier.
        This returns a list of project mappings, which contain the
        identifier in the requesting portal and the OpenPortal project
        identifier used internally.
        """
        if not isinstance(portal, openportal.PortalIdentifier):
            raise openportal.OpenPortalError(f"Invalid portal identifier: {portal}")

        mappings = []

        for project in models.ManagedProject.objects.filter(
            destination=str(self.destination())
        ):
            if not project.has_remote_identifier():
                continue

            remote_identifier = project.get_remote_identifier()

            if remote_identifier.portal_identifier != portal:
                # This project is not in the requested portal
                continue

            if project.has_local_identifier():
                mappings.append(project.get_mapping())
            else:
                mappings.append(openportal.ProjectMapping(f"{remote_identifier}:None"))

        logger.info(f"Mappings for portal {portal}: {mappings}")

        return mappings

    def get_project_mapping(
        self, identifier: openportal.ProjectIdentifier
    ) -> openportal.ProjectMapping:
        """
        Get the mapping for a project in OpenPortal with the given identifier.
        This returns the mapping from the identifier in the requesting portal
        to the OpenPortal project identifier used internally.
        """
        if not isinstance(identifier, openportal.ProjectIdentifier):
            raise openportal.OpenPortalError(
                f"Invalid project identifier: {identifier}"
            )

        # Get the ManagedProject for this identifier, which must already exist
        try:
            managed_project = models.ManagedProject.objects.get(
                identifier=str(identifier),
                destination=str(self.destination()),
            )
        except models.ManagedProject.DoesNotExist:
            logger.error(f"ManagedProject for identifier {identifier} does not exist.")
            raise openportal.OpenPortalError(
                f"ManagedProject for identifier '{identifier}' does not exist"
            )

        if managed_project.project is None:
            logger.error(
                f"ManagedProject {managed_project} does not have an associated project."
            )
            raise openportal.OpenPortalError(
                f"ManagedProject '{managed_project}' does not have an associated project"
            )

        project = managed_project.project

        if project.is_expired or project.is_removed:
            # we can't make any changes to this project - return an error
            logger.error(f"ManagedProject {managed_project} is expired or removed.")
            raise openportal.OpenPortalError(
                f"ManagedProject '{managed_project}' is expired or removed"
            )

        return managed_project.get_mapping()

    def _get_cached_report_for_month(self, project, month: int, year: int):
        """
        Return a CachedProjectUsageReport-derived ProjectUsageReport for the given
        project, month, and year, or None if no cached data exists.

        Uses _identifiers_for_project_uuid to find all project identifiers,
        including cases where the Allocation has been deleted (slug-based fallback).
        Falls back to InvoiceItem if no cached reports are found.
        """
        from .filters import _identifiers_for_project_uuid

        project_identifiers = _identifiers_for_project_uuid(project.uuid)

        if not project_identifiers:
            return None

        cached_records = models.CachedProjectUsageReport.objects.filter(
            project_identifier__in=project_identifiers,
            year=year,
            month=month,
        )

        if not cached_records.exists():
            return None

        reports = [cr.get_report() for cr in cached_records]

        if len(reports) == 1:
            return reports[0]

        return openportal.ProjectUsageReport.combine(reports)

    def get_usage_report(
        self,
        identifier: openportal.ProjectIdentifier,
        date_range: openportal.DateRange,
    ) -> openportal.UsageReport:
        """
        Get a usage report for a project in OpenPortal for the given date range.
        This returns the usage report, which contains the usage data for the project.
        """
        if not isinstance(identifier, openportal.ProjectIdentifier):
            raise openportal.OpenPortalError(
                f"Invalid project identifier: {identifier}"
            )

        if not isinstance(date_range, openportal.DateRange):
            raise openportal.OpenPortalError(f"Invalid date range: {date_range}")

        # Get the ManagedProject for this identifier, which must already exist
        try:
            managed_project = models.ManagedProject.objects.get(
                identifier=str(identifier),
                destination=str(self.destination()),
            )
        except models.ManagedProject.DoesNotExist:
            logger.error(f"ManagedProject for identifier {identifier} does not exist.")
            raise openportal.OpenPortalError(
                f"ManagedProject for identifier '{identifier}' does not exist"
            )

        if managed_project.project is None:
            logger.error(
                f"ManagedProject {managed_project} does not have an associated project."
            )
            raise openportal.OpenPortalError(
                f"ManagedProject '{managed_project}' does not have an associated project"
            )

        project = managed_project.project

        if project.is_removed:
            # we can't make any changes to this project - return an error
            logger.error(f"ManagedProject {managed_project} is removed.")
            raise openportal.OpenPortalError(
                f"ManagedProject '{managed_project}' is removed"
            )

        # Get the units we should report the usage in - this should be the
        # same unit as used for the project allocation
        details = managed_project.get_details()

        scale_factor = 1.0

        if details.allocation is not None:
            allocation_units = details.allocation.units

            if allocation_units is not None:
                template = managed_project.get_project_template()

                try:
                    scale_factor = template.get_allocation_mapping_for(allocation_units)
                except Exception as e:
                    logger.warning(
                        f"Failed to get the allocation mapping for {allocation_units} from template {template}: {e}"
                    )

        report = openportal.ProjectUsageReport(managed_project.get_remote_identifier())
        this_month = date.today().month
        this_year = date.today().year

        # Get the usage month by month
        for month_range in date_range.months:
            month = month_range.start_date.month
            year = month_range.start_date.year

            # Try to use CachedProjectUsageReport first, if all allocations
            # for this project are OpenPortal-managed with project identifiers
            cached = self._get_cached_report_for_month(project, month, year)

            if cached is not None:
                logger.info(
                    f"Using cached usage report for project {project} for {month}/{year}"
                )

                # Build {UserIdentifier: email} map for remap_users.
                # resolve_useridentifiers works on strings; we keep the
                # original UserIdentifier objects to pass to remap_users.
                user_identifiers = cached.users
                uid_strings = [str(uid) for uid in user_identifiers]
                user_info_map = utils.resolve_useridentifiers(uid_strings)

                user_email_map = {}
                for uid in user_identifiers:
                    user_info = user_info_map.get(str(uid))
                    if user_info and user_info.get("email"):
                        user_email_map[uid] = user_info["email"]

                if user_email_map:
                    cached.remap_users(user_email_map)

                # remap_project updates the project identifier and rebuilds
                # all UserIdentifier keys so that report += cached succeeds.
                remote_id = managed_project.get_remote_identifier()
                cached.remap_project(remote_id)

                if year <= this_year and month < this_month and not cached.is_complete:
                    # this is a month in the past - we don't expect the usage to change
                    logger.warning(
                        f"Cached usage report for project {project} for {month}/{year} is not marked complete, but this month is in the past. Will need to refetch in the future to get the completed report."
                    )

                report += cached
            else:
                # Fall back to building usage from InvoiceItem objects
                logger.info(
                    f"Fetching invoice items for project {project} for {month}/{year}"
                )

                try:
                    invoice_items = invoice_models.InvoiceItem.objects.filter(
                        project=project, invoice__month=month, invoice__year=year
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to retrieve invoice items for project {project}: {e}"
                    )
                    invoice_items = []

                for invoice_item in invoice_items:
                    usage = float(invoice_item.price)

                    logger.info(f"Invoice {invoice_item} : Usage {usage}")

                    if usage == 0:
                        continue
                    elif usage < 0:
                        # this is a credit, so can be safely ignored here
                        continue

                    # get the month and year of the usage
                    try:
                        invoice_month = invoice_item.invoice.month
                        invoice_year = invoice_item.invoice.year
                    except Exception:
                        logger.warning(
                            f"Invoice item {invoice_item} has no invoice month/year - skipping"
                        )
                        continue

                    if invoice_month is None or invoice_year is None:
                        logger.warning(
                            f"Invoice item {invoice_item} has no invoice month/year - skipping"
                        )
                        continue

                    if invoice_month < 1 or invoice_month > 12:
                        logger.warning(
                            f"Invoice item {invoice_item} has invalid month {invoice_month} - skipping"
                        )
                        continue

                    if invoice_month != month or invoice_year != year:
                        logger.warning(
                            f"Invoice item {invoice_item} has mismatched month/year - skipping"
                        )
                        continue

                    consumption_date = date(year=year, month=month, day=1)

                    # change the day to the first of the month
                    consumption_date = utils.get_first_day_of_month(consumption_date)

                    # But make sure that the consumption date fits within
                    # the date range - this is messy as we don't have day-based
                    # consumption from the invoice
                    if consumption_date < date_range.start_date:
                        consumption_date = date_range.start_date
                    elif consumption_date > date_range.end_date:
                        consumption_date = date_range.end_date

                    d = openportal.DailyProjectUsageReport()
                    d.add_unattributed_usage(openportal.Usage.from_hours(usage))

                    if year <= this_year and month < this_month:
                        # this is a month in the past - we don't expect the usage to change
                        d.set_complete()

                    report.add_report(consumption_date, d)

        if scale_factor is None or scale_factor <= 0:
            logger.warning(f"Invalid scale factor: {scale_factor}")
        elif scale_factor != 1.0:
            report.scale_total(scale_factor)

        # now filter the report to the requested date range
        return report.filter(date_range)

    def get_usage_reports(
        self, portal: openportal.PortalIdentifier, date_range: openportal.DateRange
    ) -> openportal.UsageReport:
        """
        Return a usage report that covers all of the projects managed by the
        specified portal.
        """
        if not isinstance(portal, openportal.PortalIdentifier):
            raise openportal.OpenPortalError(f"Invalid portal identifier: {portal}")

        reports = []

        for project in models.ManagedProject.objects.filter(
            destination=str(self.destination())
        ):
            if not project.has_remote_identifier():
                continue

            remote_identifier = project.get_remote_identifier()

            if remote_identifier.portal_identifier != portal:
                # This project is not in the requested portal
                continue

            if project.has_local_identifier():
                reports.append(
                    self.get_usage_report(
                        identifier=remote_identifier,
                        date_range=date_range,
                    ).to_usage_report()
                )

        return openportal.UsageReport.combine(reports)

    def get_storage_report(
        self,
        identifier: openportal.ProjectIdentifier,
        date_range: openportal.DateRange,
    ) -> openportal.ProjectStorageReport:
        """
        Return the accumulated storage report for a managed project over the given
        date range.  Only CachedProjectStorageReport records are used — there is no
        InvoiceItem fallback, and no unit scaling.
        """
        if not isinstance(identifier, openportal.ProjectIdentifier):
            raise openportal.OpenPortalError(
                f"Invalid project identifier: {identifier}"
            )

        if not isinstance(date_range, openportal.DateRange):
            raise openportal.OpenPortalError(f"Invalid date range: {date_range}")

        logger.info(
            f"Getting storage report for project {identifier} and date range {date_range}"
        )

        try:
            managed_project = models.ManagedProject.objects.get(
                identifier=str(identifier),
                destination=str(self.destination()),
            )
        except models.ManagedProject.DoesNotExist:
            raise openportal.OpenPortalError(
                f"ManagedProject for identifier '{identifier}' does not exist"
            )

        if managed_project.project is None:
            raise openportal.OpenPortalError(
                f"ManagedProject '{managed_project}' does not have an associated project"
            )

        project = managed_project.project

        if project.is_removed:
            raise openportal.OpenPortalError(
                f"ManagedProject '{managed_project}' is removed"
            )

        from .filters import _identifiers_for_project_uuid

        project_identifiers = _identifiers_for_project_uuid(project.uuid)

        if not project_identifiers:
            raise openportal.OpenPortalError(
                f"No project identifiers found for project {project}"
            )

        report = openportal.ProjectStorageReport(
            managed_project.get_remote_identifier()
        )

        logger.info(f"Date range: {date_range}")
        logger.info(f"report = {report}")

        # Get the storage month by month
        for month_range in date_range.months:
            month = month_range.start_date.month
            year = month_range.start_date.year

            cached_records = models.CachedProjectStorageReport.objects.filter(
                project_identifier__in=project_identifiers,
                year=year,
                month=month,
            )

            if not cached_records.exists():
                logger.info(
                    f"No cached storage report for project {project}"
                    f" for {month}/{year} - skipping"
                )
                continue

            logger.info(
                f"Using cached storage report for project {project} for {month}/{year}"
            )

            records = [cr.get_report() for cr in cached_records]
            monthly = (
                records[0]
                if len(records) == 1
                else openportal.ProjectStorageReport.combine(records)
            )

            # Remap unix usernames to email addresses
            user_identifiers = monthly.users
            uid_strings = [str(uid) for uid in user_identifiers]
            user_info_map = utils.resolve_useridentifiers(uid_strings)
            user_email_map = {}
            for uid in user_identifiers:
                user_info = user_info_map.get(str(uid))
                if user_info and user_info.get("email"):
                    user_email_map[uid] = user_info["email"]
            if user_email_map:
                monthly.remap_users(user_email_map)

            # remap_project updates the project identifier and rebuilds
            # all UserIdentifier keys so that report += monthly succeeds.
            monthly.remap_project(managed_project.get_remote_identifier())
            report += monthly

        # Filter to the exact requested date range (trims partial months
        # at either end of the requested range).
        return report.filter(date_range)

    def get_storage_reports(
        self, portal: openportal.PortalIdentifier, date_range: openportal.DateRange
    ) -> openportal.StorageReport:
        """
        Return a storage report that covers all of the projects managed by the
        specified portal.
        """
        if not isinstance(portal, openportal.PortalIdentifier):
            raise openportal.OpenPortalError(f"Invalid portal identifier: {portal}")

        reports = []

        for project in models.ManagedProject.objects.filter(
            destination=str(self.destination())
        ):
            if not project.has_remote_identifier():
                continue

            remote_identifier = project.get_remote_identifier()

            if remote_identifier.portal_identifier != portal:
                # This project is not in the requested portal
                continue

            if project.has_local_identifier():
                reports.append(
                    self.get_storage_report(
                        identifier=remote_identifier,
                        date_range=date_range,
                    ).to_storage_report()
                )

        return openportal.StorageReport.combine(reports)

    def send_result(self, job: openportal.Job) -> None:
        """
        Send the result of a job back to OpenPortal.
        """
        logger.info(f"Sending result for job {job}")

        if not isinstance(job, openportal.Job):
            raise openportal.OpenPortalError(f"Invalid job: {job}")

        openportal.send_result(job)
