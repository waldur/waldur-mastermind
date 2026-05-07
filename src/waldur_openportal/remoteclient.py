import datetime
import logging

import openportal

from waldur_core.structure import models as structure_models
from waldur_slurm.structures import Account

from . import exceptions
from . import utils as openportal_utils
from .client import OpenPortalRunner

logger = logging.getLogger(__name__)


class RemoteOpenPortalClient:
    """
    This class implements Python client for OpenPortal,
    when handling remote OpenPortal instances.
    See also: https://github.com/isambard-sc/openportal
    """

    def __init__(self, instance_name, project_template):
        if instance_name is None:
            raise exceptions.OpenPortalError("Instance name cannot be None")

        if project_template is None:
            logger.warning(
                "Project class is not specified, using default 'default' project class"
            )
            project_template = "default"

        self._runner = OpenPortalRunner()
        self._destination = openportal.Destination(instance_name)
        self._project_template = openportal.ProjectTemplate(project_template)

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

    def _to_project_details(self, details) -> openportal.AwardDetails:
        """
        Convert the passed (any) object into a ProjectDetails object
        """
        if not isinstance(details, openportal.AwardDetails):
            details = openportal.AwardDetails(str(details))

        details.project_template = self._project_template
        return details

    def _to_usage(self, usage) -> openportal.Usage:
        """
        Convert the passed (any) object into a Usage object
        """
        if not isinstance(usage, openportal.Usage):
            usage = openportal.Usage.parse(str(usage))

        return usage

    def health(self) -> openportal.Health:
        """
        Check the health of the OpenPortal system
        """
        self._runner.health()

    def destination(self) -> openportal.Destination:
        """
        Return the destination that identifies the instance that
        is being managed by this client
        """
        return self._destination

    def portal(self) -> openportal.PortalIdentifier:
        """
        Return the name of the portal that holds the instance that
        is being managed by this client
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

    def add_user(
        self, shortname: str, project: openportal.ProjectIdentifier
    ) -> openportal.UserMapping:
        """
        Tell OpenPortal to add the specified short (unix) name to the project.
        The username should be unique on the caller
        side. OpenPortal will derive its own internal username for this user,
        based on the passed username and project, which will be returned by
        this method once the user has been added
        """
        project = self._to_project_identifier(project)

        if (not shortname) or (not shortname.strip()):
            raise exceptions.OpenPortalError(f"Invalid empty username '{shortname}'")

        user = openportal.UserIdentifier(f"{shortname}.{project}")

        mapping = self.run(f"{self.destination()} add_user {user}")

        logger.info(
            f"Added OpenPortal user to project {project} with mapping {mapping}"
        )

        return mapping

    def delete_user(self, user: openportal.UserIdentifier) -> None:
        """
        Remove the OpenPortal user with specified UserIdentifier
        """
        user = self._to_user_identifier(user)

        self.run(f"{self.destination()} remove_user {user}")

        logger.info(f"Deleted OpenPortal user '{user}'")

    def _get_project_shortname(self, project: structure_models.Project) -> str:
        """
        Return the preferred shortname for the passed project.
        """
        shortname = openportal_utils.get_project_shortname(project)

        if shortname is None:
            logger.warning(
                f"Project {project} does not have a shortname set - using default"
            )
            shortname = openportal_utils.set_default_project_shortname(project)

        if shortname is None or len(shortname.strip()) == 0:
            logger.error(f"Empty shortname for project: {project}")
            raise exceptions.OpenPortalOtherError(
                f"Project {project} does not have a valid shortname set."
            )

        return shortname

    def get_project_identifier(
        self, project: structure_models.Project
    ) -> openportal.ProjectIdentifier:
        if project is None:
            raise exceptions.OpenPortalOtherError("Project cannot be None")

        project_shortname = self._get_project_shortname(project)

        if project_shortname is None or not project_shortname.strip():
            logger.error(
                f"Empty project_shortname for project: {project} - cannot create in OpenPortal"
            )
            raise exceptions.OpenPortalOtherError(
                f"Empty project_shortname for project. Please set a short name for {project}"
            )

        return self._to_project_identifier(project_shortname)

    def add_project(
        self, project: openportal.ProjectIdentifier, details: openportal.AwardDetails
    ) -> openportal.ProjectMapping:
        """
        Tell OpenPortal to create a project with the specified name.
        This name should be unique on the caller side. OpenPortal will
        derive a unique internal name for this project based on that
        name, and will create it, and return the mapping
        """
        project = self._to_project_identifier(project)
        details = self._to_project_details(details)

        logger.info(
            f"Creating remote OpenPortal project {project} with details {details} on destination {self.destination()}"
        )

        mapping = self.run(f"{self.destination()} create_project {project} {details}")

        logger.info(
            f"Created remote OpenPortal project {project} with details {details} and mapping {mapping}"
        )

        return mapping

    def update_project(
        self,
        project: openportal.ProjectIdentifier,
        details: openportal.AwardDetails,
    ) -> openportal.ProjectMapping:
        """
        Update the project with the specified name and details.
        This will update the project details, but not the project name.
        """
        project = self._to_project_identifier(project)
        details = self._to_project_details(details)

        logger.info(
            f"Updating remote OpenPortal project {project} with details {details}"
        )

        mapping = self.run(f"{self.destination()} update_project {project} {details}")

        logger.info(
            f"Updated remote OpenPortal project {project} with details {details} and mapping {mapping}"
        )

        return mapping

    def delete_project(self, project: openportal.ProjectIdentifier):
        """
        Delete the project with the specified name.
        """
        project = self._to_project_identifier(project)

        self.run(f"{self.destination()} remove_project {project}")

    def set_resource_limits(
        self, project: openportal.ProjectIdentifier, limit: openportal.Usage
    ) -> openportal.Usage:
        """
        Set the resource usage limit for the specified project to the specified limit.
        This returns the limit that has actually been set.
        """
        project = self._to_project_identifier(project)
        usage = self._to_usage(limit)

        logger.info(f"Setting resource usage limit for project {project} to {usage}")

        logger.info(f"{self.destination()} set_limit {project} {usage.seconds} seconds")

        new_limit = self.run(
            f"{self.destination()} set_limit {project} {usage.seconds} seconds"
        )

        new_limit = usage

        return new_limit

    def get_resource_limits(
        self, project: openportal.ProjectIdentifier
    ) -> openportal.Usage:
        """
        Get the resource usage limit for the specified project
        """
        project = self._to_project_identifier(project)

        limit = self.run(f"{self.destination()} get_limit {project}")

        return limit

    def get_usage_report(
        self, project: openportal.ProjectIdentifier, date_range: openportal.DateRange
    ):
        """
        Return the usage report for the specified project and date range
        """
        project = self._to_project_identifier(project)

        report = self.run(
            f"{self.destination()} get_usage_report {project} {date_range}"
        )
        return report

    def get_storage_report(
        self,
        project: openportal.ProjectIdentifier,
        date_range: openportal.DateRange,
    ) -> "openportal.ProjectStorageReport":
        """
        Return the accumulated storage report for the specified project and date range
        """
        project = self._to_project_identifier(project)

        report = self.run(
            f"{self.destination()} get_storage_report {project} {date_range}"
        )
        return report

    def get_users(
        self, project: openportal.ProjectIdentifier
    ) -> list[openportal.UserMapping]:
        """
        Return the mappings for all users on the resource for the specified project
        """
        project = self._to_project_identifier(project)
        return self.run(f"{self.destination()} get_users {project}")

    def run(self, command: str):
        """
        Run the passed command and await the result
        """
        logger.debug(f"Running command '{command}'")
        op_job = self._runner.run(command)

        now = datetime.datetime.now()
        last_update = now

        while not op_job.wait(2500):
            check_time = datetime.datetime.now()

            if check_time - last_update > datetime.timedelta(seconds=10):
                total_duration = check_time - now
                logger.warning(
                    f"Job {command} is still running... for {total_duration}"
                )
                last_update = check_time

            if check_time - now > datetime.timedelta(seconds=30):
                logger.error(f"Job {command} is taking too long to run - skipping!")
                break

        # Give the job another 100ms to finish...
        if not op_job.wait(100):
            logger.error(f"Job {command} timed out - skipping!")
            raise exceptions.OpenPortalError(f"Job '{command}' timed out - skipping!")

        if op_job.is_error:
            logger.error(f"Job {command} has failed: {op_job.error_message}")
            raise exceptions.convert_to_openportal_error(op_job.error_message)
        else:
            logger.debug(f"Job finished: {command} - SUCCESS")
            return op_job.result

    def list_accounts(self) -> list[Account]:
        """
        Return the Account objects for all projects active on the resource
        """
        projects = self.run(f"{self.destination()} get_projects")
        return [
            Account(name=project, description="", organization="")
            for project in projects
        ]

    def create_account(
        self, name: str, description: str, organization: str, parent_name: str = None
    ) -> openportal.ProjectMapping:
        """
        Create an account with the specified name, description, organization and parent account.
        However, OpenPortal only cares about the project name, and ignores the description,
        organisation and parent name. This just creates a project with the specified name,
        returning the project mapping for that project
        """
        logger.info(
            f"Creating account '{name}' with description '{description}' and organization '{organization}'"
        )

        if parent_name is not None:
            logger.warning(
                f"Ignoring parent_name '{parent_name}' as OpenPortal does not support account hierarchies"
            )

        return self.add_project(name)
