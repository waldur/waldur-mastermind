import json
import logging

import openportal
from django.conf import settings
from django.core import validators
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import F
from django.utils.translation import gettext_lazy as _
from model_utils import FieldTracker

from waldur_core.core import models as core_models
from waldur_core.core.enums import ReviewStates
from waldur_core.core.mixins import ReviewMixin
from waldur_core.permissions.models import Role, UserRole
from waldur_core.structure import models as structure_models
from waldur_core.structure.managers import get_project_users
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_openportal import utils

from . import config

logger = logging.getLogger(__name__)

MAX_USER_SHORTNAME_LENGTH = 32
MAX_PROJECT_SHORTNAME_LENGTH = 30
MAX_GROUPNAME_LENGTH = 64
MAX_USERNAME_LENGTH = 64
MAX_USERIDENTIFIER_LENGTH = 128
MAX_PROJECTIDENTIFIER_LENGTH = 64
MAX_PORTALIDENTIFIER_LENGTH = 32
MAX_PROJECTCLASS_LENGTH = 128
MAX_ALLOWED_DESTINATIONS_LENGTH = 1024
MAX_DESTINATION_LENGTH = 256
MAX_OFFERING_LENGTH = 256
MAX_KEY_LENGTH = 256


class OnceTask(models.Model):
    """
    This model is responsible for storing data about tasks that should
    only be run once, e.g. to create a project class or an allocation.
    """

    task_name = models.CharField(
        max_length=128, unique=True, verbose_name=_("task name"), db_index=True
    )

    # The date when this task was started
    last_run = models.DateTimeField(
        verbose_name=_("run date"),
        help_text=_("The date when this task was started."),
        blank=True,
        null=True,
    )

    def __str__(self) -> str:
        return self.task_name

    def __repr__(self) -> str:
        return self.__str__()


class UsageMixin(models.Model):
    class Meta:
        abstract = True

    # This is the number of node hours used
    node_usage = models.DecimalField(default=0, decimal_places=2, max_digits=20)


class Allocation(UsageMixin, structure_models.BaseResource, core_models.AvailableMixin):
    class Meta(structure_models.BaseResource.Meta):
        pass

    is_active = models.BooleanField(default=True)
    tracker = FieldTracker()

    node_limit = models.BigIntegerField(default=0)

    # The local group name for the group to which all members of the
    # allocation will be added (e.g. the Unix group)
    groupname = models.TextField(
        max_length=MAX_GROUPNAME_LENGTH,
        blank=True,
        null=True,
    )

    # Whether or not the project has been successfully added to OpenPortal
    is_added = models.BooleanField(default=False)

    @classmethod
    def get_url_name(cls):
        return "openportal-allocation"

    def is_added_to_openportal(self):
        return self.is_added

    def usage_changed(self):
        return any(self.tracker.has_changed(field) for field in utils.FIELD_NAMES)

    def has_local_group(self) -> bool:
        return bool(self.groupname)

    def get_local_group(self) -> str:
        return self.groupname

    def has_project_identifier(self) -> bool:
        return bool(self.backend_id)

    def set_project_identifier(self, project: openportal.ProjectIdentifier):
        if not isinstance(project, openportal.ProjectIdentifier):
            project = openportal.ProjectIdentifier(project)

        if self.has_project_identifier():
            if project != self.get_project_identifier():
                raise ValueError(
                    f"Project {project} does not match allocation {self.get_project_identifier()}"
                )

            return

        self.backend_id = str(project)

    def get_project_identifier(self) -> openportal.ProjectIdentifier:
        if not self.has_project_identifier():
            raise ValueError("ProjectIdentifier is not set!")

        return openportal.ProjectIdentifier(self.backend_id)

    def has_mapping(self) -> bool:
        return self.has_project_identifier() and self.has_local_group()

    def get_mapping(self) -> openportal.ProjectMapping:
        if not self.has_mapping():
            raise ValueError("ProjectMapping is not set!")

        return openportal.ProjectMapping(
            f"{self.get_project_identifier()}:{self.get_local_group()}"
        )

    def set_mapping(self, mapping: openportal.ProjectMapping):
        if not isinstance(mapping, openportal.ProjectMapping):
            mapping = openportal.ProjectMapping(mapping)

        self.set_project_identifier(mapping.project)
        self.groupname = mapping.local_group

    @classmethod
    def get_backend_fields(cls):
        return super().get_backend_fields() + ("node_usage",)

    def __str__(self):
        if self.has_mapping():
            return f"{self.name}|{self.get_mapping()}"
        elif self.has_project_identifier():
            return f"{self.name}|{self.get_project_identifier()}"
        else:
            return f"{self.name} (not in OpenPortal)"

    def __repr__(self):
        return self.__str__()


class RemoteAllocation(
    UsageMixin, structure_models.BaseResource, core_models.AvailableMixin
):
    class Meta(structure_models.BaseResource.Meta):
        pass

    is_active = models.BooleanField(default=True)
    tracker = FieldTracker()

    node_limit = models.BigIntegerField(default=0)

    # The ProjectIdentifier for the project in the remote OpenPortal instance
    remote_project_identifier = models.CharField(
        max_length=MAX_PROJECTIDENTIFIER_LENGTH,
        blank=True,
        null=True,
        verbose_name=_("remote project identifier"),
        help_text=_("The identifier of the project in the remote OpenPortal instance."),
    )

    # Whether or not the project has been successfully added to OpenPortal
    is_added = models.BooleanField(default=False)

    # The current local version of the remote project
    local_version = models.PositiveIntegerField(
        default=0,
        verbose_name=_("local version"),
        help_text=_(
            "The local version of the remote project. This is used to track changes to the project."
        ),
    )

    # The last successful updated version of the remote project
    remote_version = models.PositiveIntegerField(
        default=0,
        verbose_name=_("remote version"),
        help_text=_(
            "The last successful updated version of the remote project. This is used to track changes to the project."
        ),
    )

    @classmethod
    def get_url_name(cls):
        return "openportal-remote-allocation"

    def get_version(self) -> int:
        """
        Get the current local version of the remote project.
        This is used to track changes to the project.
        """
        return int(self.local_version)

    def increment_version(self) -> int:
        """
        Update the local version of the remote project.
        This is used to track changes to the project.
        """
        self.local_version = F("local_version") + 1
        self.save(update_fields=["local_version"])
        self.refresh_from_db()
        return int(self.local_version)

    def successfully_updated(self, version: int):
        """
        Mark the remote project as successfully updated.
        This is used to track changes to the project.
        """
        if version < self.remote_version:
            # somebody has already beaten us to this update
            logger.warning(
                f"Remote project {self} has already been updated to version {self.remote_version}, not updating to {version}."
            )
            return

        if version > self.local_version:
            # we cannot update to a version that is higher than our local version
            logger.error(
                f"Cannot update remote project {self} to version {version} as it is higher than the local version {self.local_version}."
            )
            raise ValueError(
                f"Cannot update remote project {self} to version {version} as it is higher than the local version {self.local_version}."
            )

        self.remote_version = version
        self.save(update_fields=["remote_version"])
        self.refresh_from_db()

    def needs_updating(self) -> bool:
        """
        Check if the remote project needs updating.
        This is done by checking if the local version is greater than the remote version.
        """
        return int(self.local_version) > self.remote_version

    def is_added_to_openportal(self):
        return self.is_added

    def usage_changed(self):
        return any(self.tracker.has_changed(field) for field in utils.FIELD_NAMES)

    def has_project_identifier(self) -> bool:
        return bool(self.backend_id)

    def set_project_identifier(self, project: openportal.ProjectIdentifier):
        if not isinstance(project, openportal.ProjectIdentifier):
            project = openportal.ProjectIdentifier(project)

        if self.has_project_identifier():
            if project != self.get_project_identifier():
                raise ValueError(
                    f"Project {project} does not match allocation {self.get_project_identifier()}"
                )

            return

        self.backend_id = str(project)

    def get_project_identifier(self) -> openportal.ProjectIdentifier:
        if not self.has_project_identifier():
            raise ValueError("ProjectIdentifier is not set!")

        return openportal.ProjectIdentifier(self.backend_id)

    def _get_requested_allocation(self):
        """
        Return the requested allocation and units from the passed allocation.
        """
        # The service_settings.options provide the fixed options for the offering
        service_options: dict = self.service_settings.options or {}

        allocation_unit = str(service_options.get("allocation_unit", "NHR")).strip()

        if len(allocation_unit) == 0:
            allocation_unit = "NHR"

        if "default_allocation" in service_options:
            try:
                default_allocation = float(service_options["default_allocation"])
            except Exception as e:
                logger.warning(f"Invalid default allocation value: {e}")
                default_allocation = None
        else:
            default_allocation = None

        if "max_allocation" in service_options:
            try:
                max_allocation = float(service_options["max_allocation"])
            except Exception as e:
                logger.warning(f"Invalid max allocation value: {e}")
                max_allocation = None
        else:
            max_allocation = None

        # Now we need to get the resource to see if any options have been set
        try:
            resource = marketplace_models.Resource.objects.get(
                uuid=self.marketplace_uuid
            )
        except marketplace_models.Resource.DoesNotExist:
            logger.warning(
                f"Resource with UUID {self.marketplace_uuid} does not exist. Cannot get allocation."
            )
            resource = None

        allocation = default_allocation

        if resource is not None:
            options = resource.options or {}

            if "allocation" in options:
                try:
                    allocation = float(options["allocation"])
                except Exception as e:
                    logger.warning(f"Invalid allocation value: {e}")
                    allocation = default_allocation

        if allocation is not None and max_allocation is not None:
            if allocation > max_allocation:
                logger.warning(
                    f"Allocation {allocation} exceeds maximum allocation {max_allocation}. Setting to maximum."
                )
                allocation = max_allocation

        if resource is not None:
            options = resource.options or {}

            if "allocation" in options:
                # make sure that the used allocation is reflected in
                # the resource options
                try:
                    if float(resource.options["allocation"]) != float(allocation):
                        logger.debug(
                            f"Updating allocation for resource {resource} to {allocation} {allocation_unit}"
                        )
                        resource.options["allocation"] = allocation
                        resource.save(update_fields=["options"])
                except Exception as e:
                    logger.warning(f"Failed to set allocation: {e}")
            else:
                try:
                    logger.debug(
                        f"Recording default allocation {allocation} for resource {resource}"
                    )
                    options["allocation"] = allocation
                    resource.options = options
                    resource.save(update_fields=["options"])
                except Exception as e:
                    logger.warning(f"Failed to set allocation: {e}")

        logger.debug(
            f"Requested credits from allocation {allocation} with unit {allocation_unit}"
        )

        return (allocation, allocation_unit)

    def get_project_details(self) -> openportal.AwardDetails:
        if self.project is None:
            raise ValueError("Project is not set!")

        project = self.project

        details = openportal.AwardDetails("{}")

        if project.name is not None:
            details.name = str(project.name)

        if project.description is not None:
            details.description = str(project.description)

        if project.start_date is not None:
            details.start_date = project.start_date

        if project.end_date is not None:
            details.end_date = project.end_date

        # The project key is the UUID of the organisation that owns
        # the project. This way, only projects within the approved
        # organisation can create remote projects using this
        # allocation, thereby preventing an admin of another
        # organisation from guessing the project template name
        # and using that
        details.key = str(project.customer.uuid)

        # now get the allocation for this project (if requested)
        allocation, allocation_unit = self._get_requested_allocation()

        if allocation is not None:
            if allocation_unit is None:
                allocation_unit = "NHR"

            try:
                details.allocation = openportal.Allocation.parse(
                    f"{allocation} {allocation_unit}"
                )
            except Exception as e:
                logger.warning(
                    f"Failed to parse allocation {allocation} {allocation_unit}: {e}"
                )

        # now try to add in any members for this project
        # also keeping note of the allowed email domains
        # for this project
        for user_id in get_project_users(project.id):
            try:
                user = core_models.User.objects.filter(id=user_id)

                if not user.exists():
                    logger.debug(
                        f"Skipping non-existing user {user_id} for project {project}"
                    )
                    continue

                user = user.first()

                if user is None:
                    logger.debug(
                        f"Skipping non-existing user {user_id} for project {project}"
                    )
                    continue

                if not user.is_active:
                    logger.debug(f"Skipping inactive user {user} for project {project}")
                    continue

                if user.email is None:
                    logger.debug(
                        f"Skipping user {user} without email for project {project}"
                    )
                    continue

                email = str(user.email).strip()

                if not email:
                    logger.debug(
                        f"Skipping user {user} with empty email for project {project}"
                    )
                    continue

                # get the role name of this user in the project
                roles = UserRole.objects.filter(
                    user=user, scope=project, is_active=True
                )

                if roles.exists():
                    if len(roles) > 1:
                        logger.warning(
                            f"User {user} has multiple roles in project {project}: {roles}. Only using the first one."
                        )

                    role = roles.first()

                    if role is None:
                        logger.warning(
                            f"User {user} has no role in project {project}. Not adding user!"
                        )
                        continue

                    role = role.role

                    if role is None:
                        logger.warning(
                            f"User {user} has no role in project {project}. Not adding user!"
                        )
                        continue

                    if role.name is None:
                        logger.warning(
                            f"User {user} has no role name in project {project}. Not adding user!"
                        )
                        continue

                    if role.description is not None:
                        role_name = str(role.description).strip()
                    else:
                        role_name = ""

                    if len(role_name) == 0 and role.name is not None:
                        role_name = str(role.name).strip()

                    if len(role_name) == 0:
                        logger.warning(
                            f"User {user} has empty role name in project {project}. Not adding user!"
                        )
                        continue

                    logger.debug(
                        f"Adding user {user} with role {role_name} to project {project}"
                    )

                    details.add_member(email, role_name)
            except Exception as e:
                logger.warning(f"Failed to add user {user} to project {project}: {e}")

        logger.debug(f"Returning project details for project {project}: {details}")

        return details

    def has_mapping(self) -> bool:
        return self.has_project_identifier() and self.has_remote_project_identifier()

    def get_mapping(self) -> openportal.ProjectMapping:
        if not self.has_mapping():
            raise ValueError("ProjectMapping is not set!")

        return openportal.ProjectMapping(
            f"{self.get_project_identifier()}:{self.get_remote_project_identifier()}"
        )

    def get_remote_project_identifier(self) -> openportal.ProjectIdentifier:
        if not self.has_remote_project_identifier():
            raise ValueError("RemoteProjectIdentifier is not set!")

        return openportal.ProjectIdentifier(self.remote_project_identifier)

    def has_remote_project_identifier(self) -> bool:
        return self.remote_project_identifier is not None

    def set_remote_project_identifier(
        self, remote_project: openportal.ProjectIdentifier
    ):
        if not isinstance(remote_project, openportal.ProjectIdentifier):
            remote_project = openportal.ProjectIdentifier(remote_project)

        if self.has_remote_project_identifier():
            if remote_project != self.get_remote_project_identifier():
                raise ValueError(
                    f"Remote project {remote_project} does not match allocation {self.get_remote_project_identifier()}"
                )

            return

        self.remote_project_identifier = str(remote_project)

    def set_mapping(self, mapping: openportal.ProjectMapping):
        if not isinstance(mapping, openportal.ProjectMapping):
            mapping = openportal.ProjectMapping(mapping)

        self.set_project_identifier(mapping.project)
        self.set_remote_project_identifier(mapping.local_group)

    def update_mapping(self, mapping: openportal.ProjectMapping):
        if not isinstance(mapping, openportal.ProjectMapping):
            mapping = openportal.ProjectMapping(mapping)

        if not self.has_mapping():
            self.set_mapping(mapping)
            return

        if mapping.project != self.get_project_identifier():
            raise ValueError(
                f"Project {mapping.project} does not match allocation {self.get_project_identifier()}"
            )

        remote_identifier = openportal.ProjectIdentifier(mapping.local_group)

        if remote_identifier != self.get_remote_project_identifier():
            logger.warning(
                f"Changing remote project identifier from {self.get_remote_project_identifier()} to {remote_identifier} for {self}"
            )

            self.set_remote_project_identifier(remote_identifier)
            self.save()

    @classmethod
    def get_backend_fields(cls):
        return super().get_backend_fields() + ("node_usage",)

    def get_backend(self, **kwargs):
        from .remotebackend import RemoteOpenPortalBackend

        return RemoteOpenPortalBackend(self.service_settings, **kwargs)

    def __str__(self):
        if self.has_mapping():
            return f"{self.name}|{self.get_mapping()}"
        elif self.has_project_identifier():
            return f"{self.name}|{self.get_project_identifier()}"
        else:
            return f"{self.name} (not in OpenPortal)"

    def __repr__(self):
        return self.__str__()


class Association(core_models.UuidMixin):
    # This is the allocation to which the user is associated.
    allocation = models.ForeignKey(
        to=Allocation,
        on_delete=models.CASCADE,
        related_name="op-associations-allocation+",
    )

    # This is the Waldur user which is associated with the allocation.
    user = models.ForeignKey(
        to=settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="op-associations-user+",
        blank=True,
        null=True,
    )

    # This is the local username on the instance being allocated
    # (e.g. the unix username)
    username = models.CharField(
        max_length=MAX_USERNAME_LENGTH,
        blank=True,
        null=True,
    )

    # This is the local groupname on the instance being allocated
    # (e.g. the primary unix group for the user)
    groupname = models.CharField(
        max_length=MAX_GROUPNAME_LENGTH,
        blank=True,
        null=True,
    )

    # This is the OpenPortal UserIdentifier that uniquely
    # identifies this user in OpenPortal
    useridentifier = models.CharField(
        max_length=MAX_USERIDENTIFIER_LENGTH, blank=True, null=True
    )

    def has_project_identifier(self) -> bool:
        return self.allocation.has_project_identifier()

    def set_project_identifier(self, project: openportal.ProjectIdentifier):
        self.allocation.set_project_identifier(project)

    def get_project_identifier(self) -> openportal.ProjectIdentifier:
        return self.allocation.get_project_identifier()

    def has_user_identifier(self) -> bool:
        return bool(self.useridentifier)

    def set_user_identifier(self, user: openportal.UserIdentifier):
        if not isinstance(user, openportal.UserIdentifier):
            user = openportal.UserIdentifier(user)

        if self.has_user_identifier():
            if user != self.get_user_identifier():
                raise ValueError(
                    f"User {user} does not match association {self.get_user_identifier()}"
                )
        else:
            self.set_project_identifier(user.project_identifier)
            self.useridentifier = str(user)

    def get_user_identifier(self) -> openportal.UserIdentifier:
        return openportal.UserIdentifier(self.useridentifier)

    def has_local_group(self) -> bool:
        return bool(self.groupname)

    def get_local_group(self) -> str:
        return self.groupname

    def has_local_user(self) -> bool:
        return bool(self.username)

    def get_local_user(self) -> str:
        return self.username

    def has_mapping(self) -> bool:
        return (
            self.has_user_identifier()
            and self.has_local_user()
            and self.has_local_group()
        )

    def set_mapping(self, mapping: openportal.UserMapping):
        if not isinstance(mapping, openportal.UserMapping):
            mapping = openportal.UserMapping(mapping)

        self.set_user_identifier(mapping.user)

        if self.has_local_user():
            if mapping.local_user != self.get_local_user():
                logger.warning(
                    f"Changing local user from {self.username} to {mapping.local_user} for {self}"
                )

        self.username = mapping.local_user

        if self.has_local_group():
            if mapping.local_group != self.get_local_group():
                logger.warning(
                    f"Changing local group from {self.groupname} to {mapping.local_group} for {self}"
                )

        self.groupname = mapping.local_group

    def get_mapping(self) -> openportal.UserMapping:
        if not self.has_user_identifier():
            raise ValueError("UserIdentifier is not set!")

        if not self.has_local_user():
            raise ValueError("Local user is not set!")

        return openportal.UserMapping(
            f"{self.get_user_identifier()}:{self.get_local_user()}:{self.get_local_group()}"
        )

    def __str__(self):
        if self.has_mapping():
            return f"{self.allocation} <-> {self.get_mapping()}"
        elif self.has_user_identifier():
            return f"{self.allocation} <-> {self.get_user_identifier()}"
        else:
            return f"{self.allocation} <-> {self.user} (not in OpenPortal)"

    def __repr__(self):
        return self.__str__()


class RemoteAssociation(core_models.UuidMixin):
    # This is the allocation to which the user is associated.
    allocation = models.ForeignKey(
        to=RemoteAllocation,
        on_delete=models.CASCADE,
        related_name="op-remote_associations-remote_allocation+",
    )

    # This is the Waldur user which is associated with the allocation.
    user = models.ForeignKey(
        to=settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="op-remote_associations-user+",
        blank=True,
        null=True,
    )

    def __str__(self):
        return f"{self.allocation} <-> {self.user}"

    def __repr__(self):
        return self.__str__()


class AllocationUserUsage(UsageMixin):
    """
    Allocation usage per user. This model is responsible for the allocation usage definition for particular user.
    """

    allocation = models.ForeignKey(
        to=Allocation,
        on_delete=models.CASCADE,
        related_name="op-allocationuser-allocation+",
    )
    year = models.PositiveSmallIntegerField()
    month = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(12)]
    )

    user = models.ForeignKey(
        to=settings.AUTH_USER_MODEL,
        related_name="op-allocationuser-user+",
        on_delete=models.CASCADE,
        blank=True,
        null=True,
    )

    # This is the local username on the instance being allocated
    # (e.g. the unix username). We can work out the OpenPortal UserIdentifier
    # from the allocation and user objects, and then the OpenPortal
    # UserMapping from that.
    username = models.CharField(max_length=MAX_USERNAME_LENGTH)

    def __str__(self):
        return f"{self.username}: {self.allocation.name}"

    def __repr__(self) -> str:
        return self.__str__()


class HistoricalAllocation(UsageMixin):
    """
    This model holds the historical usage of an allocation.
    It records the total usage per month, plus whether or not that
    month is "complete" (i.e. the report from OpenPortal is complete
    for that month, and no more usage will be added).
    """

    allocation = models.ForeignKey(
        to=Allocation, on_delete=models.CASCADE, related_name="op-historicalallocation+"
    )
    year = models.PositiveSmallIntegerField()
    month = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(12)]
    )
    is_complete = models.BooleanField(default=False)

    def __str__(self):
        if self.is_complete:
            return (
                f"{self.allocation.name} [{self.year}-{self.month}]: {self.node_usage}"
            )
        else:
            return f"{self.allocation.name} [{self.year}-{self.month}]: {self.node_usage} (incomplete)"

    def __repr__(self) -> str:
        return self.__str__()


class HistoricalRemoteAllocation(UsageMixin):
    """
    This model holds the historical usage of an allocation.
    It records the total usage per month, plus whether or not that
    month is "complete" (i.e. the report from OpenPortal is complete
    for that month, and no more usage will be added).
    """

    allocation = models.ForeignKey(
        to=RemoteAllocation,
        on_delete=models.CASCADE,
        related_name="op-remote-historicalallocation+",
    )
    year = models.PositiveSmallIntegerField()
    month = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(12)]
    )
    is_complete = models.BooleanField(default=False)

    def __str__(self):
        if self.is_complete:
            return (
                f"{self.allocation.name} [{self.year}-{self.month}]: {self.node_usage}"
            )
        else:
            return f"{self.allocation.name} [{self.year}-{self.month}]: {self.node_usage} (incomplete)"

    def __repr__(self) -> str:
        return self.__str__()


class CachedProjectUsageReport(models.Model):
    """
    Caches the full ProjectUsageReport JSON from OpenPortal for a given
    project, month, and resource (destination). This allows rich report
    data to be served to the frontend without re-fetching from OpenPortal.

    The report field stores the JSON-serialised OpenPortal ProjectUsageReport
    object. Use openportal.ProjectUsageReport.from_json(json.dumps(self.report))
    to deserialise.
    """

    year = models.PositiveSmallIntegerField()
    month = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(12)]
    )
    project_identifier = models.CharField(max_length=MAX_PROJECTIDENTIFIER_LENGTH)
    resource = models.CharField(max_length=MAX_DESTINATION_LENGTH)
    is_complete = models.BooleanField(default=False)
    report = models.JSONField()

    class Meta:
        unique_together = [("year", "month", "project_identifier", "resource")]
        indexes = [models.Index(fields=["project_identifier"])]

    def get_report(self) -> "openportal.ProjectUsageReport":
        return openportal.ProjectUsageReport.from_json(json.dumps(self.report))

    def __str__(self):
        status = "complete" if self.is_complete else "incomplete"
        return f"{self.project_identifier} [{self.year}-{self.month:02d}] ({self.resource}): {status}"

    def __repr__(self) -> str:
        return self.__str__()


class CachedProjectStorageReport(models.Model):
    """
    Caches an accumulated ProjectStorageReport JSON from OpenPortal for a given
    project, month, and resource (destination). Each time a new storage snapshot
    is fetched it is merged into the cached report via +=, so the stored object
    accumulates daily snapshots over the course of the month.

    The report field stores the JSON-serialised OpenPortal ProjectStorageReport
    object. Use openportal.ProjectStorageReport.from_json(json.dumps(self.report))
    to deserialise.
    """

    year = models.PositiveSmallIntegerField()
    month = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(12)]
    )
    project_identifier = models.CharField(max_length=MAX_PROJECTIDENTIFIER_LENGTH)
    resource = models.CharField(max_length=MAX_DESTINATION_LENGTH)
    report = models.JSONField()

    class Meta:
        unique_together = [("year", "month", "project_identifier", "resource")]
        indexes = [models.Index(fields=["project_identifier"])]

    def get_report(self) -> "openportal.ProjectStorageReport":
        return openportal.ProjectStorageReport.from_json(json.dumps(self.report))

    def __str__(self):
        return f"{self.project_identifier} [{self.year}-{self.month:02d}] ({self.resource})"

    def __repr__(self) -> str:
        return self.__str__()


class UserInfo(models.Model):
    """
    This model is responsible for storing additional user information
    required by OpenPortal. Currently this is the preferred shortname
    for the user, which will be combined with the project shortname
    to create the local username
    """

    user = models.OneToOneField(
        to=settings.AUTH_USER_MODEL,
        related_name="op-userinfo-user+",
        on_delete=models.CASCADE,
    )

    shortname = models.CharField(
        verbose_name=_("shortname"),
        max_length=MAX_USER_SHORTNAME_LENGTH,
        unique=True,
        null=True,
        help_text=_(
            "A short, unique name for you. It will be used to form your local username on any systems. Should only contain lower-case letters and digits and must start with a letter."
        ),
        validators=[
            validators.RegexValidator(
                regex=r"^[a-z][a-z0-9]+$",
                message="Must start with a letter and only contain numbers and letters.",
            ),
            validators.RegexValidator(
                regex=r"(admin)|(root)$",
                inverse_match=True,
            ),
            validators.MinLengthValidator(4),
            validators.MaxLengthValidator(MAX_USER_SHORTNAME_LENGTH),
        ],
    )

    tracker = FieldTracker(fields=["shortname"])

    def __str__(self) -> str:
        return f"{self.user}: {self.shortname}"

    def __repr__(self) -> str:
        return self.__str__()

    def sanitise(self):
        """
        Double check that our shortname matches the user unix_username
        if this field exists
        """
        if hasattr(self.user, "unix_username"):
            if (
                self.shortname != self.user.unix_username
                and self.user.unix_username is not None
            ):
                self.set_shortname(self.user.unix_username)
                self.save()

    def set_shortname(self, shortname: str):
        """
        Set the shortname, checking whether or not this has not already
        been set, and making sure it lines up with the unix_username if
        that field is present in the user
        """
        if not shortname:
            raise ValueError("Shortname cannot be empty.")

        if hasattr(self.user, "unix_username"):
            if (
                shortname != self.user.unix_username
                and self.user.unix_username is not None
            ):
                # Set flag to prevent circular updates when saving unix_username
                self.user._syncing_to_userinfo = True
                try:
                    self.user.unix_username = self.shortname
                    self.user.save(update_fields=["unix_username"])
                finally:
                    self.user._syncing_to_userinfo = False

            self.shortname = self.user.unix_username

        # make sure to copy the shortname to the slug
        # Set flag to prevent circular updates
        self.user._syncing_to_userinfo = True
        try:
            self.user.slug = shortname
            self.user.save(update_fields=["slug"])
        finally:
            self.user._syncing_to_userinfo = False

        if self.shortname and self.shortname != shortname:
            logger.error(
                f"Cannot change shortname of user {self.user} from {self.shortname} to {shortname}"
            )
            raise ValueError(
                f"Cannot change shortname of user {self.user} from {self.shortname} to {shortname}"
            )

        self.shortname = shortname

    def save(self, *args, **kwargs):
        if "update_fields" in kwargs and "query_field" not in kwargs["update_fields"]:
            kwargs["update_fields"] = set(kwargs["update_fields"]).add("query_field")

        # The shortname cannot be changed after creation as external systems may already depend on it.
        prev = self.tracker.previous("shortname")
        if self.tracker.has_changed("shortname") and prev:
            new = self.shortname
            raise ValueError(
                _(
                    f"Cannot change shortname of user ('{prev}' → '{new}') after creation."
                )
            )

        super().save(*args, **kwargs)


class ProjectShortNameGenerator(models.Model):
    """
    This model is responsible for generating unique shortnames for projects.
    It keeps track of the last used shortname and increments it for the next
    project.
    """

    shortname = models.CharField(
        max_length=MAX_PROJECT_SHORTNAME_LENGTH,
        verbose_name=_("shortname"),
        unique=True,
        help_text=_("The key / descriptor used to identify this generator."),
    )

    count = models.PositiveIntegerField(
        default=0,
        verbose_name=_("count"),
        help_text=_("The number of projects created with this shortname."),
    )

    def __str__(self) -> str:
        return f"{self.shortname}: Count = {self.count}"

    def __repr__(self) -> str:
        return self.__str__()

    def increment_count(self):
        """
        Increment the count of projects created with this shortname.
        """
        self.count = F("count") + 1
        self.save(update_fields=["count"])
        self.refresh_from_db()

    def get_shortname(self) -> str:
        """
        Get the shortname for the current value of count.
        """

        # turn the number into a letter, e.g. 0 -> a, 1 -> b, ..., 25 -> z,
        # 26 -> aa, 27 -> ab, etc.
        count = int(self.count)

        if count < 0:
            raise ValueError("Count cannot be negative.")

        shortname = ""
        while count >= 0:
            shortname = chr((count % 26) + ord("a")) + shortname
            count = count // 26 - 1

        return self.shortname.replace("{count}", shortname)


class ProjectInfo(models.Model):
    """
    This model is responsible for storing additional project information
    required by OpenPortal. Currently this is the shortname for the project,
    which will be combined with the user shortname to create the local
    username on a system. It also contains the list of allowable
    destinations of instances that can be attached to this project.
    For example, a project may only allow "brics.aip1.*", meaning that
    only instances that start with "brics.aip1." can be attached to
    this project.
    """

    project = models.OneToOneField(
        to=structure_models.Project,
        related_name="op-projectinfo-project+",
        on_delete=models.CASCADE,
    )

    shortname = models.CharField(
        verbose_name=_("shortname"),
        max_length=MAX_PROJECT_SHORTNAME_LENGTH,
        unique=True,
        null=True,
        help_text=_(
            "A short, unique name for the project. It will be used to form the local username of any users in the project on any systems. Should only contain lower-case letters and digits and must start with a letter."
        ),
        validators=[
            validators.RegexValidator(
                regex=r"^[a-z0-9\-_]+$",
            ),
            validators.RegexValidator(
                regex=r"(-admin)|(-root)$",
                inverse_match=True,
            ),
            validators.MinLengthValidator(3),
            validators.MaxLengthValidator(MAX_PROJECT_SHORTNAME_LENGTH),
        ],
    )

    # This is the list of allowable destinations of instances that can be attached to this project.
    # For example, a project may only allow "brics.aip1.*", meaning that only instances that start with
    # "brics.aip1." can be attached to this project.
    allowed_destinations = models.TextField(
        verbose_name=_("allowed destinations"),
        max_length=MAX_ALLOWED_DESTINATIONS_LENGTH,
        help_text=_(
            "A comma-separated list of allowable destinations of instances that \
             can be attached to this project. For example, a project may only allow \
             'brics.aip1.*', meaning that only instances that start with 'brics.aip1.' \
             can be attached to this project."
        ),
        blank=True,
        null=True,
    )

    tracker = FieldTracker(fields=["shortname", "allowed_destinations"])

    def __str__(self) -> str:
        return f"{self.project}: {self.shortname}"

    def __repr__(self) -> str:
        return self.__str__()

    def sanitise(self):
        """
        Enforce that the shortname set here is copied back to the
        project short_name field, if such a field exists
        (it will be removed in the future)
        """
        if hasattr(self.project, "short_name"):
            if self.shortname is None and self.project.short_name is not None:
                # copy if from the project (for legacy projects)
                shortname = self.project.short_name.strip()

                if len(shortname) == 0:
                    logger.warning(
                        f"Project {self.project} has an empty short_name, using slug instead."
                    )
                    shortname = self.project.slug.strip()

                if len(shortname) > MAX_PROJECT_SHORTNAME_LENGTH:
                    logger.warning(
                        f"Project shortname {shortname} is too long, truncating to {MAX_PROJECT_SHORTNAME_LENGTH} characters."
                    )
                    shortname = shortname[:MAX_PROJECT_SHORTNAME_LENGTH]

                self.shortname = shortname

                try:
                    self.save()
                except Exception as e:
                    logger.warning(
                        f"Failed to save project shortname {self.shortname} for project {self.project} - using slug: {e}"
                    )
                    shortname = self.project.slug.strip()

                    if len(shortname) == 0:
                        raise ValueError(
                            "Project shortname cannot be empty. Please set it in OpenPortal."
                        )

                    if len(shortname) > MAX_PROJECT_SHORTNAME_LENGTH:
                        logger.warning(
                            f"Project shortname {shortname} is too long, truncating to {MAX_PROJECT_SHORTNAME_LENGTH} characters."
                        )
                        shortname = shortname[:MAX_PROJECT_SHORTNAME_LENGTH]

                    self.shortname = shortname
                    self.save(force_accept_changed_shortname=True)

                    self.project.short_name = shortname
                    self.project.save()

            elif self.shortname is not None:
                # copy to the project so that it matches
                if self.project.short_name is None:
                    self.project.short_name = self.shortname
                    self.project.save()
                else:
                    if self.shortname != self.project.short_name:
                        logger.warning(
                            f"Project {self.project} has a different short_name ({self.project.short_name}) than the one set in OpenPortal ({self.shortname})."
                        )

                    self.project.short_name = self.shortname
                    self.project.save()
            else:
                # no shortname set - need to get it from the slug
                shortname = self.project.slug.strip()
                logger.warning(
                    f"No shortname set for project {self.project} - using slug: {shortname}"
                )

                if len(shortname) == 0:
                    raise ValueError(
                        "Project shortname cannot be empty. Please set it in OpenPortal."
                    )

                if len(shortname) > MAX_PROJECT_SHORTNAME_LENGTH:
                    logger.warning(
                        f"Project shortname {shortname} is too long, truncating to {MAX_PROJECT_SHORTNAME_LENGTH} characters."
                    )
                    shortname = shortname[:MAX_PROJECT_SHORTNAME_LENGTH]

                self.shortname = shortname
                self.save(force_accept_changed_shortname=True)

                try:
                    self.project.short_name = shortname
                    self.project.save()
                except Exception as e:
                    logger.warning(
                        f"Failed to save project shortname {self.shortname} for project {self.project} - using slug: {e}"
                    )

        elif self.shortname is None:
            # automatically generate a shortname if this is not set
            shortname = self.project.slug.strip()
            logger.warning(
                f"No shortname set for project {self.project} - using slug: {shortname}"
            )

            if len(shortname) == 0:
                raise ValueError(
                    "Project shortname cannot be empty. Please set it in OpenPortal."
                )

            if len(shortname) > MAX_PROJECT_SHORTNAME_LENGTH:
                logger.warning(
                    f"Project shortname {shortname} is too long, truncating to {MAX_PROJECT_SHORTNAME_LENGTH} characters."
                )
                shortname = shortname[:MAX_PROJECT_SHORTNAME_LENGTH]

            self.shortname = shortname
            self.save(force_accept_changed_shortname=True)

            try:
                self.project.short_name = shortname
                self.project.save()
            except Exception as e:
                logger.warning(
                    f"Failed to save project shortname {self.shortname} for project {self.project} - using slug: {e}"
                )

        if self.shortname is None:
            # Raise an error as we don't have a shortname set!
            raise ValueError(
                "Project shortname cannot be empty. Please set it in OpenPortal."
            )

    def set_shortname(self, shortname: str, force: bool = False):
        """
        Set the shortname, checking whether or not this has not already
        been set - note that it cannot be changed after creation
        as external systems may already depend on it.

        Only use 'force=True' if you are sure that you want to
        change the shortname, as this will bypass the checks
        """
        if not shortname:
            raise ValueError("Shortname cannot be empty.")

        shortname = shortname.strip()

        if len(shortname) == 0 or len(shortname) > MAX_PROJECT_SHORTNAME_LENGTH:
            raise ValueError(
                f"Shortname must be between 1 and {MAX_PROJECT_SHORTNAME_LENGTH} characters long."
            )

        self.shortname = shortname
        self.save(force_accept_changed_shortname=force)
        self.sanitise()

        if self.project:
            # Set flag to prevent circular updates
            self.project._syncing_to_projectinfo = True
            try:
                if hasattr(self.project, "short_name"):
                    if self.project.short_name != shortname:
                        try:
                            self.project.short_name = shortname
                            self.project.save(update_fields=["short_name"])
                        except Exception as e:
                            logger.warning(
                                f"Failed to save project short_name {shortname} for project {self.project}: {e}"
                            )

                # Only copy the shortname to the slug if we are NOT in
                # application_portal_only mode (i.e., we're in Project Management mode)
                try:
                    application_portal_only = core_models.Feature.objects.get(
                        key="deployment.application_portal_only"
                    ).value
                except core_models.Feature.DoesNotExist:
                    # Default to False if feature flag doesn't exist
                    application_portal_only = False

                if not application_portal_only:
                    self.project.slug = shortname
                    self.project.save(update_fields=["slug"])
            finally:
                self.project._syncing_to_projectinfo = False

    def has_shortname(self) -> bool:
        """
        Check if the project has a shortname set.
        This is used to determine if we need to generate a new shortname.
        """
        return self.shortname is not None and len(self.shortname) > 0

    def get_shortname(self) -> str:
        """
        Get the shortname for the project.
        This will raise an error if the shortname is not set.
        """
        if self.shortname is not None:
            return self.shortname.strip()
        else:
            raise ValueError("Project shortname is not set!")

    def generate_shortname(self, generator: ProjectShortNameGenerator) -> str:
        """
        Generate a shortname using the provided generator.
        This will generate a unique shortname based on the
        rules in the generator
        """
        if self.shortname is not None:
            logger.warning(
                f"Project {self.project} already has a shortname set ({self.shortname})."
                " Not generating a new one."
            )
            return self.shortname

        if not isinstance(generator, ProjectShortNameGenerator):
            raise ValueError(
                "Generator must be an instance of ProjectShortNameGenerator"
            )

        while True:
            shortname = generator.get_shortname()

            try:
                self.set_shortname(shortname, force=True)
                return shortname
            except Exception as e:
                logger.warning(
                    f"Failed to set shortname {shortname} - is it already taken? {e}"
                )

            # If we fail, check that this was because the shortname
            # was already taken, and if so, increment the generator
            # and try again.
            projects = ProjectInfo.objects.filter(
                shortname=shortname,
            )

            if projects.exists():
                logger.warning(
                    f"Shortname {shortname} already exists. Incrementing generator."
                )
                generator.increment_count()
            else:
                logger.error(
                    f"Failed to generate shortname {shortname} for project {self.project}. "
                    "This should not happen, please check the generator."
                )
                raise ValueError(
                    f"Failed to generate shortname {shortname} for project {self.project}."
                )

    def set_allowed_destinations(self, destinations: str):
        """
        Set the allowed destinations of instances that can be attached to this project.
        This should be a comma-separated list of destinations.
        """
        if not destinations:
            self.allowed_destinations = None
        else:
            self.allowed_destinations = str(destinations)

    def save(self, *args, force_accept_changed_shortname: bool = False, **kwargs):
        if "update_fields" in kwargs and "query_field" not in kwargs["update_fields"]:
            kwargs["update_fields"] = set(kwargs["update_fields"]).add("query_field")

        # The shortname cannot be changed after creation as external systems may already depend on it.
        if not force_accept_changed_shortname:
            prev = self.tracker.previous("shortname")
            if self.tracker.has_changed("shortname") and prev:
                new = self.shortname
                raise ValueError(
                    _(
                        f"Cannot change shortname of project ('{prev}' → '{new}') after creation."
                    )
                )

        super().save(*args, **kwargs)


class ProjectNotification(models.Model):
    """
    This model is responsible for storing data about when and how often
    members of a project should be sent notifications about the current
    spending of the project and the end date.

    This is here rather than in invoices or notifications as our notification
    needs are quite bespoke, we will use data that comes directly from
    OpenPortal, and we need to isolate this code from the evolution of the
    rest of Waldur. Note that we could migrate this functionality into
    core Waldur if desired.
    """

    # Which project does this relate to?
    project = models.OneToOneField(
        to=structure_models.Project,
        related_name="op-projectnotification-project+",
        on_delete=models.CASCADE,
    )

    # How many days between notifications (0 = no notifications)
    # We default to sending notifications every fortnight
    frequency = models.PositiveSmallIntegerField(
        verbose_name=_("frequency"),
        default=14,
        help_text=_("How many days between notifications (0 = no notifications)."),
    )

    # When was the last notification sent?
    # This is a date, not a datetime, as we don't care about the time
    # of the notification, just the date.
    last_notification = models.DateField(
        verbose_name=_("last notification"),
        blank=True,
        null=True,
        help_text=_("When was the last notification sent?"),
    )

    def __str__(self) -> str:
        return f"{self.project}: Last notification {self.last_notification}, frequency {self.frequency}"

    def __repr__(self) -> str:
        return self.__str__()


class Job(models.Model):
    """
    This model is responsible for storing data about jobs that are
    running in OpenPortal. This is used to track the progress of jobs
    and to ensure that we don't run the same job multiple times.
    """

    job_id = models.CharField(
        max_length=36, unique=True, verbose_name=_("ID"), db_index=True
    )

    job_data = models.TextField(
        verbose_name=_("job data"),
        help_text=_("JSON representation of the job"),
        blank=True,
        null=True,
    )

    class State(models.TextChoices):
        PENDING = "pending", _("Pending")
        RUNNING = "running", _("Running")
        COMPLETED = "completed", _("Completed")
        COMMUNICATED = "communicated", _("Communicated")
        CANCELLED = "cancelled", _("Cancelled")

    state = models.CharField(
        max_length=20,
        choices=State.choices,
        default=State.PENDING,
        verbose_name=_("status"),
        help_text=_("The current status of the job."),
    )

    # record the date when this job was created
    created = models.DateField(
        auto_now_add=True,
        verbose_name=_("created"),
        help_text=_("The date when this job was created."),
    )

    def get_job(self) -> openportal.Job:
        """
        Get the job object from the job data.
        If the job data is not set, return None.
        """
        return openportal.Job.from_json(self.job_data)

    def __str__(self) -> str:
        try:
            j = openportal.Job.from_json(self.job_data)
            return str(j)
        except Exception as e:
            logger.error(f"Failed to parse job data for job {self.job_id}: {e}")
            return f"Job {self.job_id}: Invalid data"

    def __repr__(self) -> str:
        return self.__str__()


class ProjectTemplate(core_models.UuidMixin, models.Model):
    """
    This model is responsible for storing data about project classes.
    A ProjectTemplate represents a category or type of project that can
    be created via OpenPortal. It is up to Waldur to map the ProjectTemplate
    combined with the calling portal to the Organisation that the project
    should be created in. In addition, the ProjectTemplate controls
    which portals can create which types of projects.
    """

    # The name of the project template, e.g. "ukri"
    name = models.CharField(
        max_length=MAX_PROJECTCLASS_LENGTH,
        verbose_name=_("name"),
        db_index=False,
        unique=False,
    )

    # The offering for which this template applies, e.g. "isambard-ai"
    offering = models.CharField(
        max_length=MAX_OFFERING_LENGTH,
        verbose_name=_("offering"),
        help_text=_("The offering for which this template applies."),
        blank=True,
        null=True,
    )

    # The customer (organisation) that owns this project class and can
    # make changes or approve / deny project requests for this class
    # A user needs to be a staff member, or an owner or manager of both
    # the provider and customer organisations to be able to approve or
    # deny project requests for this class. Only staff members, or
    # owners or managers of the provider organisation can make changes
    # to this project class.
    provider = models.ForeignKey(
        to=structure_models.Customer,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="op-projectclass-provider+",
        verbose_name=_("provider"),
        help_text=_("The organisation that owns this project class."),
    )

    # The name of the portal (PortalIdentifier) that is allowed to create
    # this project class, e.g. "airr".
    # The combination of name, destination and portal must be unique.
    portal = models.CharField(
        max_length=MAX_PORTALIDENTIFIER_LENGTH, verbose_name=_("portal"), db_index=False
    )

    # The key that is used to authenticate requests for this class
    key = models.CharField(
        max_length=MAX_KEY_LENGTH,
        verbose_name=_("key"),
        help_text=_("The key that is used to authenticate requests for this class."),
        blank=True,
        null=True,
    )

    # The customer (organisation) in which to place projects which are created in
    # this project class by the specified portal.
    customer = models.ForeignKey(
        to=structure_models.Customer,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="op-projectclass-organisation+",
        verbose_name=_("organisation"),
        help_text=_("The organisation that this project class belongs to."),
    )

    # The shortname naming scheme for projects created in this class
    # by the specified portal. This should have "{year}" which will replaced
    # by the last digit of the current year, and "{count}", which will be
    # replaced by a letter (a, b, c, ..., z, aa, ab, ..., az, ba, bb, ...),
    # for example, "a{year}{count}" would become "a5a", "a5b", "a5c", etc.
    shortname = models.CharField(
        max_length=MAX_PROJECT_SHORTNAME_LENGTH,
        verbose_name=_("shortname"),
        null=True,
        blank=True,
    )

    # The list of MarketPlace Offerings that should be created automatically
    # for projects created in this class by the specified portal.
    offerings = models.ManyToManyField(
        marketplace_models.Offering,
        related_name="op-projectclass-offerings+",
        verbose_name=_("offerings"),
        help_text=_(
            "The list of MarketPlace Offerings that should be created automatically for projects created in this class by the specified portal."
        ),
    )

    # The credit limit beyond which requests need to be approved by a local admin
    # If this is None, then no local approval is required. If this is set to 0,
    # then all requests (including creating the project) need to be approved.
    approval_limit = models.DecimalField(
        decimal_places=2,
        max_digits=20,
        null=True,
        blank=True,
        verbose_name=_("approval limit"),
        help_text=_(
            "The credit limit beyond which requests need to be approved by a local admin. If this is None, then no local approval is required. If this is set to 0, then all requests (including creating the project) need to be approved."
        ),
    )

    # The maximum credit limit for any projects created in this class.
    # Any requests beyond this limit are automatically rejected.
    # If this is None, then no maximum limit is set. If this is
    # set to 0, then no projects can be created in this class.
    max_credit_limit = models.DecimalField(
        decimal_places=2,
        max_digits=20,
        null=True,
        blank=True,
        verbose_name=_("maximum credit limit"),
        help_text=_(
            "The maximum credit limit for any projects created in this class. Any requests beyond this limit are automatically rejected. If this is None, then no maximum limit is set. If this is set to 0, then no projects can be created in this class."
        ),
    )

    # The mapping of role names from the remote portal to role names
    # in this portal for users in projects created in this class.
    # This is a dictionary mapping role names
    role_mapping = models.JSONField(
        verbose_name=_("role mapping"),
        default=dict,
        help_text=_(
            "The mapping of role names from the remote portal to role information in this portal for users in projects created in this class."
        ),
    )

    # The mapping of credits to allocation units, e.g. how many GPU hours
    # to award per credit allocated
    allocation_units_mapping = models.JSONField(
        verbose_name=_("allocation units mapping"),
        default=dict,
        help_text=_(
            "The mapping of credits to allocation units, i.e. how many allocation units to award per credit allocated."
        ),
    )

    # Combination of name, offering and portal must be unique
    class Meta:
        unique_together = ("name", "offering", "portal")
        ordering = ["name", "id"]
        verbose_name = _("Project class")
        verbose_name_plural = _("Project classes")

    def __str__(self) -> str:
        return f"{self.portal} <=> {self.name}@{self.offering}"

    def __repr__(self) -> str:
        return self.__str__()

    def _get_role(self, role) -> Role:
        """
        Helper method to get a Role object from the role mapping.
        If the role does not exist, raise an error.
        """
        if isinstance(role, Role):
            return role

        if isinstance(role, str):
            # Try the UUID first, then the name
            try:
                return Role.objects.get(uuid=role)
            except Role.DoesNotExist:
                return Role.objects.get(name=role)

        if isinstance(role, dict):
            # If it's a dict, it should have 'name' or 'uuid'
            if "uuid" in role:
                try:
                    return Role.objects.get(uuid=role["uuid"])
                except Role.DoesNotExist:
                    pass

            if "name" in role:
                return Role.objects.get(name=role["name"])

        logger.warning(f"Role {role} does not exist in this portal.")
        raise ValueError(f"Role {role} does not exist in this portal.")

    def get_role_mapping(self) -> dict[str, Role]:
        """
        Get the role mapping for this project class.
        This returns a dictionary mapping role names from the remote portal
        to role names in this portal for users in projects created in this class.
        """
        mappings = {}

        for remote_role, local_role in self.role_mapping.items():
            try:
                mappings[remote_role] = self._get_role(local_role)
            except Role.DoesNotExist:
                logger.warning(
                    f"Role {local_role} does not exist in this portal. Skipping mapping for {remote_role}."
                )

        return mappings

    def add_role_mapping(self, remote_role: str, local_role: Role):
        """
        Add a role mapping for this project class.
        This maps a role name from the remote portal to a role name in this portal
        for users in projects created in this class.
        """
        local_role = self._get_role(local_role)

        if remote_role in self.role_mapping:
            logger.warning(
                f"Role mapping for {remote_role} already exists. Overwriting with {local_role.name}."
            )

        # We save both the name and UUID in case the role name changes in the future.
        # We search for UUID first, then name
        self.role_mapping[remote_role] = {
            "name": local_role.name,
            "uuid": local_role.uuid,
        }
        self.save(update_fields=["role_mapping"])

    def get_local_role_for(self, remote_role: str) -> Role:
        """
        Get the local role for a given remote role in this project class.
        If the remote role does not exist, raise an error.
        """
        if remote_role not in self.role_mapping:
            raise ValueError(f"Remote role {remote_role} does not exist in this class.")

        try:
            return self._get_role(self.role_mapping[remote_role])
        except Role.DoesNotExist:
            raise ValueError(
                f"Local role {self.role_mapping[remote_role]} does not exist in this portal."
            )

    def get_allocation_units_mapping(self) -> dict[str, float]:
        """
        Get the allocation units mapping for this project class.
        This returns a dictionary mapping allocation unit names to credit values.
        """
        return self.allocation_units_mapping

    def get_allocation_mapping_for(self, allocation_unit: str) -> float:
        """
        Get the credit value for a given allocation unit in this project class.
        If the allocation unit does not exist, raise an error.
        """
        if allocation_unit not in self.allocation_units_mapping:
            canonical_unit = openportal.Allocation.canonicalize(allocation_unit)

            if (
                canonical_unit == allocation_unit
                and allocation_unit.lower().endswith("s")
                and len(allocation_unit) > 1
            ):
                canonical_unit = openportal.Allocation.canonicalize(
                    allocation_unit[:-1]
                )

            if canonical_unit not in self.allocation_units_mapping:
                raise ValueError(
                    f"Allocation unit {allocation_unit} does not exist in this class. "
                    f"Available allocation units: {list(self.allocation_units_mapping.keys())}"
                )

            return self.allocation_units_mapping[canonical_unit]
        else:
            return self.allocation_units_mapping[allocation_unit]

    def create_allocation_mappings_from_node(self, node: openportal.Node):
        """
        Create allocation mappings for a given node.
        This will create a mapping for each allocation unit in the node's
        allocation units, using the credit value from the project class.
        """
        if not isinstance(node, openportal.Node):
            raise ValueError("Node must be an instance of openportal.Node")

        mappings = {
            "NHR": 1.0,
            "GPUHR": 1.0 / node.gpus if node.gpus > 0 else 1.0,
            "CPUHR": 1.0 / node.cpus if node.cpus > 0 else 1.0,
            "COREHR": 1.0 / node.cores if node.cores > 0 else 1.0,
            "GBHR": 1.0 / node.memory_gb if node.memory_gb > 0 else 1.0,
        }

        self.allocation_units_mapping = mappings
        self.save(update_fields=["allocation_units_mapping"])

    def add_allocation_mapping(self, allocation_unit: str, credit_value: float):
        """
        Add an allocation mapping for this project class.
        This maps an allocation unit name to a credit value.
        If the allocation unit already exists, it will be overwritten.
        """
        if not isinstance(allocation_unit, str):
            raise ValueError("Allocation unit must be a string.")

        if not isinstance(credit_value, int | float):
            raise ValueError("Credit value must be a number.")

        self.allocation_units_mapping[allocation_unit] = float(credit_value)
        self.save(update_fields=["allocation_units_mapping"])

    def convert_to_credits(self, allocation: openportal.Allocation | None) -> float:
        """
        Convert the given allocation to credits based on the allocation units mapping.
        If the allocation units are not found in the mapping, it defaults to 1:1 mapping.
        """
        if allocation is None:
            return 0.0
        elif allocation.units in self.allocation_units_mapping:
            scale_factor = self.allocation_units_mapping[allocation.units]

            if scale_factor <= 0:
                logger.warning(
                    f"Allocation units {allocation.units} have a non-positive scale factor ({scale_factor}). Defaulting to 0 mapping."
                )
                return 0.0

            return allocation.size / scale_factor
        else:
            logger.warning(
                f"Allocation units {allocation.units} not found in project class {self.name}. Defaulting to 1 to 1 mapping."
            )
            return allocation.size

    def get_offering(self) -> str:
        """
        Get the offering for this project class.
        If the offering is not set, return None.
        """
        if self.offering is None:
            raise ValueError("ProjectTemplate offering is not set!")

        return self.offering.strip()

    def get_portal(self) -> openportal.PortalIdentifier:
        """
        Get the portal for this project class.
        If the portal is not set, return None.
        """
        if self.portal is None:
            raise ValueError("ProjectTemplate portal is not set!")

        return openportal.PortalIdentifier(self.portal.strip())

    def action_needs_approval(
        self, allocation: openportal.Allocation | float | None = None
    ) -> bool:
        """
        Check if the action needs approval based on the passed allocation.
        If the approval limit is None, then no approval is needed.
        If the approval limit is 0, then all actions need to be approved.
        """
        if self.approval_limit is None and self.max_credit_limit is None:
            return False

        if isinstance(allocation, float):
            # If allocation is a float, treat it as credits directly
            credits = allocation
        elif isinstance(allocation, openportal.Allocation):
            # If allocation is an Allocation object, convert it to credits
            credits = self.convert_to_credits(allocation)
        elif allocation is None:
            # If allocation is None, treat it as 0 credits
            credits = 0.0

        return self.exceeds_approval_limit(credits) or self.exceeds_max_credit_limit(
            credits
        )

    def action_is_rejected(
        self, allocation: openportal.Allocation | float | None = None
    ) -> bool:
        """
        Check if the action is rejected based on the passed allocation.
        If the maximum credit limit is None, then nothing is automatically
        rejected. If the maximum credit limit is 0, then all actions are
        rejected. Otherwise, only requests that involve requesting more
        credits than the maximum credit limit are rejected.
        """
        if self.max_credit_limit is None:
            return False

        if isinstance(allocation, float):
            # If allocation is a float, treat it as credits directly
            credits = allocation
        elif isinstance(allocation, openportal.Allocation):
            # If allocation is an Allocation object, convert it to credits
            credits = self.convert_to_credits(allocation)
        elif allocation is None:
            # If allocation is None, treat it as 0 credits
            credits = 0.0

        return self.exceeds_max_credit_limit(credits)

    def exceeds_max_credit_limit(self, credit_limit: float) -> bool:
        """
        Check if the given credit limit exceeds the maximum credit limit for this project class.
        If the maximum credit limit is None, then no maximum limit is set.
        If the maximum credit limit is 0, then no projects can be created in this class.
        """
        if self.max_credit_limit is None:
            return False
        elif self.max_credit_limit == 0:
            return True
        else:
            return credit_limit > float(self.max_credit_limit)

    def exceeds_approval_limit(self, credit_limit: float) -> bool:
        """
        Check if the given credit limit exceeds the approval limit for this project class.
        If the approval limit is None, then no local approval is required.
        If the approval limit is 0, then all requests (including creating the project) need to be approved.
        """
        if self.approval_limit is None:
            return False
        elif self.approval_limit == 0:
            return True
        else:
            return credit_limit > float(self.approval_limit)

    def get_generator(self) -> ProjectShortNameGenerator:
        """
        Get the ProjectShortNameGenerator for this project class.
        If it does not exist, create it.
        """
        if self.shortname is None:
            raise ValueError(
                "Project class shortname is not set. Please set it before generating a shortname."
            )

        shortname = self.shortname.strip()

        if len(shortname) == 0:
            raise ValueError("Project class shortname cannot be empty.")

        # Now replace the {year} with the last digit of the current year,
        # e.g. "myproject-{year}" becomes "myproject-5" for 2025.
        if "{year}" in shortname:
            from datetime import datetime

            current_year = datetime.now().year
            shortname = shortname.replace("{year}", str(current_year % 10))

        # Get (or create if needed) a new ProjectShortNameGenerator
        # for this year
        generator, created = ProjectShortNameGenerator.objects.get_or_create(
            shortname=shortname
        )

        if created:
            logger.info(f"Created new ProjectShortNameGenerator: {generator}")

        return generator

    def assert_matching_key(self, key: str):
        """
        Assert that the given key matches the key for this project class.
        If the keys do not match, raise an error.
        If the project class key is None, then no key is required.
        """
        if self.key is None:
            return

        if key is None or str(self.key) != str(key):
            raise ValueError("Invalid key for project class.")


class ManagedProject(ReviewMixin, models.Model):
    """
    This model is responsible for storing data about projects that are
    managed by OpenPortal. This is used to track the progress of projects
    and to ensure that we don't create the same project multiple times.
    """

    # This is the destination used to send instructions from the
    # remote portal to this portal
    destination = models.CharField(
        max_length=MAX_DESTINATION_LENGTH,
        verbose_name=_("destination"),
        help_text=_(
            "The destination used to send instructions from the remote portal."
        ),
        blank=False,
        null=False,
    )

    # This is the OpenPortal ProjectIdentifier from the portal that
    # requested and manages this project
    identifier = models.CharField(
        max_length=MAX_PROJECTIDENTIFIER_LENGTH,
        unique=False,
        verbose_name=_("ID"),
        db_index=False,
    )

    # It is the combination of the destination and identifier that
    # should be unique and used to identify the project
    class Meta:
        unique_together = ("destination", "identifier")
        verbose_name = _("Managed Project")
        verbose_name_plural = _("Managed Projects")

    # This is the JSON representation of the OpenPortal AwardDetails
    # that is synced between this portal and the managing portal
    details = models.JSONField(
        verbose_name=_("project data"),
        help_text=_("JSON representation of the project"),
        blank=True,
        null=True,
    )

    # This is the actual project in this Waldur
    project = models.ForeignKey(
        to=structure_models.Project,
        on_delete=models.SET_NULL,
        related_name="op-managedproject-project+",
        verbose_name=_("project"),
        blank=True,
        null=True,
    )

    # This is the ProjectTemplate that this project belongs to.
    project_template = models.ForeignKey(
        to=ProjectTemplate,
        on_delete=models.SET_NULL,
        related_name="op-managedproject-projectclass+",
        verbose_name=_("project class"),
        blank=True,
        null=True,
    )

    # This is the ProjectIdentifier for this project in this portal
    local_identifier = models.CharField(
        max_length=MAX_PROJECTIDENTIFIER_LENGTH,
        verbose_name=_("local ID"),
        blank=True,
        null=True,
        help_text=_("The local project identifier in this portal."),
    )

    def get_destination(self) -> openportal.Destination:
        """
        Get the destination for this project.
        If the destination is not set, raise an error.
        """
        if self.destination is None:
            raise ValueError("Destination is not set for this project.")

        return openportal.Destination(self.destination)

    def get_offering(self) -> str:
        """
        Return the name of the offering connected to this destination
        """
        return str(self.get_destination().agents[-1])

    def has_project_template(self) -> bool:
        """
        Check if this project has a project class set.
        This is used to determine whether or not the project
        can be created in this portal.
        """
        return self.project_template is not None

    def get_project_template(self) -> ProjectTemplate:
        """
        Get the ProjectTemplate for this project.
        This will try to resolve the project template
        from the information in the project details if
        one is not set
        """
        if self.project_template is None:
            details = self.get_details()

            if details.project_template is None:
                logger.error(
                    "Project does not have a project template set and no project template is defined in the project details."
                )
                self.delete()
                raise ValueError(
                    "Project does not have a project template set and no project template is defined in the project details."
                )

            try:
                remote_identifier = self.get_remote_identifier()
            except Exception:
                logger.error(
                    "Project does not have a remote identifier set, cannot resolve project template."
                )
                self.delete()
                raise ValueError(
                    "Project does not have a remote identifier set, cannot resolve project template."
                )

            remote_portal = remote_identifier.portal

            try:
                self.project_template = ProjectTemplate.objects.get(
                    name=details.project_template,
                    portal=remote_portal,
                )
            except ProjectTemplate.DoesNotExist:
                logger.error(
                    f"Project template {details.project_template} for portal {remote_portal} does not exist."
                )
                self.delete()
                raise ValueError(
                    f"Project template {details.project_template} for portal {remote_portal} does not exist."
                )

            self.save(update_fields=["project_template"])

        return self.project_template

    def set_project_template(self, project_template: ProjectTemplate):
        """
        Set the ProjectTemplate for this project.
        If the project class is not an instance of ProjectTemplate, raise an error.
        """
        if not isinstance(project_template, ProjectTemplate):
            raise ValueError("Project class must be an instance of ProjectTemplate.")

        if self.project_template and self.project_template != project_template:
            logger.warning(
                f"Project class for project {self.project} is being changed from {self.project_template} to {project_template}."
            )

        self.project_template = project_template

    def has_local_identifier(self) -> bool:
        """
        Check if this project has a local identifier set.
        This is used to determine whether or not the project
        can be created in this portal.
        """
        return self.local_identifier is not None and len(self.local_identifier) > 0

    def get_local_identifier(self) -> openportal.ProjectIdentifier:
        """
        Get the local ProjectIdentifier for this project.
        If the local identifier is not set, raise an error.
        """
        if not self.local_identifier:
            raise ValueError("Local identifier is not set for this project.")
        return openportal.ProjectIdentifier(self.local_identifier)

    def set_local_identifier(self, identifier: openportal.ProjectIdentifier):
        """
        Set the local ProjectIdentifier for this project.
        If the identifier is not an instance of ProjectIdentifier, raise an error.
        """
        if not isinstance(identifier, openportal.ProjectIdentifier):
            raise ValueError("Identifier must be an instance of ProjectIdentifier.")

        if self.local_identifier and self.local_identifier != str(identifier):
            logger.warning(
                f"Local identifier for project {self.project} is being changed from {self.local_identifier} to {identifier}."
            )

        self.local_identifier = str(identifier)

    def has_remote_identifier(self) -> bool:
        """
        Check if this project has a remote identifier set.
        This is used to determine whether or not the project
        can be created in this portal.
        """
        return self.identifier is not None and len(self.identifier) > 0

    def get_remote_identifier(self) -> openportal.ProjectIdentifier:
        """
        Get the remote ProjectIdentifier for this project.
        If the identifier is not set, raise an error.
        """
        if not self.identifier:
            raise ValueError("Remote identifier is not set for this project.")
        return openportal.ProjectIdentifier(self.identifier)

    def set_remote_identifier(self, identifier: openportal.ProjectIdentifier):
        """
        Set the remote ProjectIdentifier for this project.
        If the identifier is not an instance of ProjectIdentifier, raise an error.
        """
        if not isinstance(identifier, openportal.ProjectIdentifier):
            raise ValueError("Identifier must be an instance of ProjectIdentifier.")

        if self.identifier and self.identifier != str(identifier):
            logger.warning(
                f"Remote identifier for project {self.project} is being changed from {self.identifier} to {identifier}."
            )

        self.identifier = str(identifier)

    def get_mapping(self) -> openportal.ProjectMapping:
        """
        Get the ProjectMapping object for this project.
        If the project data is not set, raise an error.
        """
        return openportal.ProjectMapping(
            f"{self.get_remote_identifier()}:{self.get_local_identifier()}"
        )

    def set_details(self, details: openportal.AwardDetails):
        """
        Set the AwardDetails object for this project.
        If the details are not an instance of AwardDetails, convert it.
        """
        if not isinstance(details, openportal.AwardDetails):
            if not isinstance(details, str):
                details = openportal.AwardDetails(json.dumps(details))
            else:
                details = openportal.AwardDetails(details)

        new_details = json.loads(str(details))

        if not isinstance(new_details, dict):
            raise ValueError(
                "Project details must be a dict, not a string or other type."
            )

        if self.details != new_details:
            old_details = self.details
            self.details = new_details
            self.save(update_fields=["details"])

            last = (
                ManagedProjectAuditEntry.objects.filter(
                    managed_project=self,
                    event_type=ManagedProjectAuditEventType.DETAILS_UPDATED,
                )
                .order_by("-timestamp")
                .first()
            )
            if last is None or last.new_details != self.details:
                ManagedProjectAuditEntry.record(
                    self,
                    ManagedProjectAuditEventType.DETAILS_UPDATED,
                    previous_details=old_details,
                    new_details=self.details,
                )

    def get_details(self) -> openportal.AwardDetails:
        """
        Get the AwardDetails object from the project data.
        If the project data is not set, return None.
        """
        return openportal.AwardDetails(json.dumps(self.details))

    def _get_project_link(self) -> openportal.Link | None:
        """
        Get the project link for the attached project, or None
        if there is no such project
        """
        if self.project is not None:
            from waldur_core.core.utils import format_homeport_link

            link = openportal.Link()
            link.id = str(self.project.slug)
            try:
                link.url = format_homeport_link(f"/projects/{self.project.uuid}/")
            except Exception:
                link.url = None
            return link
        else:
            return None

    def set_project(self, project: structure_models.Project | None):
        """
        Set the project for this managed project.
        This will also update the project link in the project details to point
        to the new project.
        If the project is not an instance of Project, raise an error.
        """
        if project is None:
            self.project = None
            self.save(update_fields=["project"])
            existing_details = self.get_details()
            existing_details.project_link = None
            self.set_details(existing_details)
            return

        if not isinstance(project, structure_models.Project):
            raise ValueError("Project must be an instance of Project.")

        if self.project and self.project != project:
            logger.warning(
                f"Project for managed project {self} is being changed from {self.project} to {project}."
            )

        self.project = project
        self.save(update_fields=["project"])
        existing_details = self.get_details()
        existing_details.project_link = self._get_project_link()
        self.set_details(existing_details)

    def merge_details(
        self, new_details: openportal.AwardDetails
    ) -> openportal.AwardDetails:
        """
        Merge incoming details from the local portal into the existing details,
        with the following fields treated as authoritative from the incoming
        details and always overriding the stored values:

        - membership_control, allowed_domains: the remote portal sets membership
          policy and it must not be softened by a merge with older stored values.
        - award, call, renewal: link fields set by the local portal are the
          source of truth and must replace whatever was previously stored.
        - members: a None value explicitly means "do not manage membership" and
          must not be overridden by stored members from a previous sync.

        project_link is always set from this portal's own project reference,
        since the local portal has no knowledge of the remote project URL/slug.
        """
        existing = self.get_details()
        merged = existing.merge(new_details)

        # These fields are authoritative from the incoming details and must
        # override the merged result.
        merged.membership_control = new_details.membership_control
        merged.allowed_domains = new_details.allowed_domains
        merged.award = new_details.award
        merged.call = new_details.call
        merged.renewal = new_details.renewal
        merged.earliest_approve = new_details.earliest_approve

        if new_details.members is None:
            merged.members = None

        merged.project_link = self._get_project_link()
        return merged

    def get_default_offerings(self) -> list[marketplace_models.Offering]:
        """
        Get the default marketplace offerings for this project.
        If the project class is not set, return an empty list.
        """
        if self.project_template and self.project_template.offerings.exists():
            return list(self.project_template.offerings.all())
        else:
            return []

    def set_needs_approval(
        self, needs_approval: bool = True, comment: str | None = None
    ):
        """
        Set whether or not this project needs approval from a local admin.
        This is used to control whether or not changes to this project
        need to be approved by a local admin.
        """
        if needs_approval:
            if self.state != ReviewStates.PENDING:
                self.state = ReviewStates.PENDING
                self.reviewed_by = None
                self.reviewed_at = None
                self.review_comment = comment
                self.save(
                    update_fields=[
                        "state",
                        "reviewed_by",
                        "reviewed_at",
                        "review_comment",
                    ]
                )
        else:
            self.approve(
                utils.get_openportal_robot(), comment="Approved automatically."
            )

    def is_pending(self) -> bool:
        """
        Check if this project is pending approval.
        This is used to determine whether or not the project
        needs to be approved by a local admin.
        """
        return self.state == ReviewStates.PENDING

    def is_canceled(self) -> bool:
        """
        Check if this project is canceled.
        This is used to determine whether or not the project
        has been canceled by a local admin.
        """
        return self.state == ReviewStates.CANCELED

    def is_rejected(self) -> bool:
        """
        Check if this project is rejected.
        This is used to determine whether or not the project
        has been rejected by a local admin.
        """
        return self.state == ReviewStates.REJECTED

    def is_approved(self) -> bool:
        """
        Check if this project is approved.
        This is used to determine whether or not the project
        has been approved by a local admin.
        """
        return self.state == ReviewStates.APPROVED

    def _notify(self, action: str):
        """
        Send an award notification back to the remote portal that submitted
        this managed project.  The destination is reversed because we are
        replying in the opposite direction along the routing path.

        Failures are logged and swallowed so they never disrupt the caller.
        """
        if not config.ensure_config_loaded():
            return
        try:
            config.ensure_config_loaded()
            dest = openportal.Destination(self.destination)
            reverse_dest = openportal.Destination(".".join(reversed(dest.agents)))
            openportal.notify(f"{reverse_dest} {action} {self.identifier}")
        except Exception as e:
            logger.warning(
                f"Failed to send {action!r} notification for ManagedProject "
                f"{self.identifier!r} to {self.destination!r}: {e}"
            )

    def notify_added(self):
        self._notify("award_added")

    def notify_changed(self):
        self._notify("award_changed")

    def notify_removed(self):
        self._notify("award_removed")

    def notify_accepted(self):
        self._notify("award_accepted")

    def notify_rejected(self):
        self._notify("award_rejected")

    def __str__(self) -> str:
        try:
            return f"ManagedProject for {self.get_offering()} [{self.identifier} => {self.project}]"
        except Exception:
            return f"ManagedProject for 'null offering' [{self.identifier} => {self.project}]"


# ---------------------------------------------------------------------------
# RemoteProject — local portal representation of a project on a remote portal
# ---------------------------------------------------------------------------


class MembershipControlChoices:
    """
    Controls whether the receiving portal may independently modify
    project membership or roles.  Mirrors the MembershipControl enum
    in the OpenPortal AwardDetails grammar.
    """

    OPEN = "open"
    MEMBERS_ONLY = "members_only"
    ROLES_ONLY = "roles_only"
    LOCKED = "locked"

    CHOICES = [
        (OPEN, _("Open — receiving portal manages membership freely")),
        (MEMBERS_ONLY, _("Members only — roles are authoritative")),
        (ROLES_ONLY, _("Roles only — membership is authoritative")),
        (LOCKED, _("Locked — both membership and roles are authoritative")),
    ]


class RemoteProjectState:
    """
    State machine for RemoteProject.

    PENDING  — AwardDetails have been sent to the remote portal but we have
               not yet received confirmation (may be awaiting human review).
    ACTIVE   — The award has been approved, a project exists on the remote
               portal, and we have received confirmation that everything is ok.
    STALE    — We have not heard from the remote portal for an unexpectedly
               long time; the state may be out of date.
    ERROR    — The request was rejected or the connection to the remote portal
               is definitively broken.
    DELETED  — The local resource associated with this award has been deleted.
               The record is retained for history; a future resource may be
               reconnected to revive this RemoteProject.
    """

    PENDING = "pending"
    ACTIVE = "active"
    STALE = "stale"
    ERROR = "error"
    DELETED = "deleted"

    CHOICES = [
        (PENDING, _("Pending")),
        (ACTIVE, _("Active")),
        (STALE, _("Stale")),
        (ERROR, _("Error")),
        (DELETED, _("Deleted")),
    ]


class ManagedProjectAuditEventType(models.TextChoices):
    """Event types for ManagedProjectAuditEntry."""

    CREATED = "created", _("Project request received")
    APPROVED = "approved", _("Project request approved")
    REJECTED = "rejected", _("Project request rejected")
    DELETED = "deleted", _("ManagedProject deleted")
    NOTE_ADDED = "note_added", _("Note added")
    DETAILS_UPDATED = "details_updated", _("Award details updated")
    PROJECT_ATTACHED = "project_attached", _("Waldur project attached")
    PROJECT_DETACHED = "project_detached", _("Waldur project detached")


class ManagedProjectAuditEntry(models.Model):
    """
    Permanent audit log for a ManagedProject.

    Unlike RemoteProjectAuditEntry (which uses CASCADE), these entries survive
    the deletion of the ManagedProject itself.  The stable composite identity
    (identifier + destination) is copied from the ManagedProject at creation time
    so that history is preserved even after the ManagedProject is gone.

    The nullable FK to ManagedProject is kept for convenience while the object
    still exists.
    """

    # Copied from the ManagedProject at record() time — survive deletion
    identifier = models.CharField(
        max_length=MAX_PROJECTIDENTIFIER_LENGTH,
        verbose_name=_("identifier"),
        help_text=_("Project identifier copied from ManagedProject at record time."),
    )

    destination = models.CharField(
        max_length=MAX_DESTINATION_LENGTH,
        verbose_name=_("destination"),
        help_text=_("Destination copied from ManagedProject at record time."),
    )

    # Nullable FK — set to NULL when the ManagedProject is deleted
    managed_project = models.ForeignKey(
        to="ManagedProject",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_log",
        verbose_name=_("managed project"),
    )

    timestamp = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        verbose_name=_("timestamp"),
    )

    event_type = models.CharField(
        max_length=32,
        choices=ManagedProjectAuditEventType.choices,
        db_index=True,
        verbose_name=_("event type"),
    )

    previous_details = models.JSONField(
        blank=True,
        null=True,
        verbose_name=_("previous details"),
        help_text=_("Project details before this event.  Null for creation events."),
    )

    new_details = models.JSONField(
        blank=True,
        null=True,
        verbose_name=_("new details"),
        help_text=_(
            "Project details after this event.  "
            "Null for events that do not change the details."
        ),
    )

    performed_by = models.ForeignKey(
        to=core_models.User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("performed by"),
        help_text=_("The user who triggered this event, if it was a human action."),
    )

    note = models.TextField(
        blank=True,
        verbose_name=_("note"),
        help_text=_("Optional free-text comment about this event."),
    )

    class Meta:
        ordering = ["-timestamp", "id"]
        verbose_name = _("Managed Project Audit Entry")
        verbose_name_plural = _("Managed Project Audit Entries")

    def __str__(self) -> str:
        return f"{self.identifier}@{self.destination} — {self.event_type} at {self.timestamp}"

    def __repr__(self) -> str:
        return self.__str__()

    @classmethod
    def record(
        cls,
        managed_project: "ManagedProject",
        event_type: str,
        performed_by=None,
        note: str = "",
        previous_details=None,
        new_details=None,
    ) -> "ManagedProjectAuditEntry | None":
        """
        Create and save an audit entry for the given ManagedProject.

        Copies identifier and destination from the ManagedProject at call time
        so that the record survives deletion of the ManagedProject.

        For events that carry new_details, the entry is skipped if new_details
        is identical to the most recent entry for this project, preventing
        duplicate records when the remote portal repeatedly sends the same
        unchanged request.
        """
        if new_details is not None and event_type in (
            ManagedProjectAuditEventType.CREATED,
            ManagedProjectAuditEventType.DETAILS_UPDATED,
        ):
            last = (
                cls.objects.filter(
                    identifier=managed_project.identifier,
                    destination=managed_project.destination,
                    new_details__isnull=False,
                )
                .order_by("-timestamp")
                .first()
            )
            if last is not None and last.new_details == new_details:
                return None

        return cls.objects.create(
            identifier=managed_project.identifier,
            destination=managed_project.destination,
            managed_project=managed_project,
            event_type=event_type,
            performed_by=performed_by,
            note=note or "",
            previous_details=previous_details,
            new_details=new_details,
        )


class RemoteProject(core_models.UuidMixin, models.Model):
    """
    Persistent record of a project on a remote OpenPortal portal, held on the
    local (award-creating) portal.

    A RemoteProject survives the deletion or replacement of the RemoteAllocation
    that is currently attached to it, preserving history and metadata even when
    the local marketplace resource is removed.

    The stable identity is (destination, identifier):
      destination — routing path, e.g. "airr.brics.isambard-ai"
      identifier  — stable remote project ID, e.g. "u6ac.brics"

    These are set on creation and never change.  If the routing address changes
    that is considered a new RemoteProject.
    """

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    destination = models.CharField(
        max_length=MAX_DESTINATION_LENGTH,
        verbose_name=_("destination"),
        help_text=_(
            "Routing path from the local portal to the remote resource, "
            "e.g. 'airr.brics.isambard-ai'.  Never changes after creation."
        ),
    )

    identifier = models.CharField(
        max_length=MAX_PROJECTIDENTIFIER_LENGTH,
        null=True,
        blank=True,
        verbose_name=_("identifier"),
        help_text=_(
            "Stable remote project identifier, e.g. 'u6ac.brics'.  "
            "Uniquely identifies the project on the remote portal and never changes.  "
            "Null while the project is pending first approval."
        ),
    )

    class Meta:
        verbose_name = _("Remote Project")
        verbose_name_plural = _("Remote Projects")
        constraints = [
            models.UniqueConstraint(
                fields=["destination", "identifier"],
                condition=models.Q(identifier__isnull=False),
                name="unique_remote_project_destination_identifier",
            ),
            models.UniqueConstraint(
                fields=["destination", "current_project"],
                condition=models.Q(
                    identifier__isnull=True,
                    current_project__isnull=False,
                ),
                name="unique_pending_remote_project_destination_project",
            ),
        ]

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    state = models.CharField(
        max_length=16,
        choices=RemoteProjectState.CHOICES,
        default=RemoteProjectState.PENDING,
        verbose_name=_("state"),
        db_index=True,
    )

    error_message = models.TextField(
        blank=True,
        default="",
        verbose_name=_("error message"),
        help_text=_(
            "The most recent rejection or error message received from the "
            "remote portal.  Cleared when the state transitions to ACTIVE."
        ),
    )

    # ------------------------------------------------------------------
    # Award details snapshots
    # ------------------------------------------------------------------

    last_sent_details = models.JSONField(
        blank=True,
        null=True,
        verbose_name=_("last sent award details"),
        help_text=_(
            "The AwardDetails most recently sent to the remote portal "
            "(create_award or update_award).  May not yet be confirmed."
        ),
    )

    last_confirmed_details = models.JSONField(
        blank=True,
        null=True,
        verbose_name=_("last confirmed award details"),
        help_text=_(
            "The AwardDetails most recently confirmed by the remote portal.  "
            "Reflects what is actually live on the remote portal."
        ),
    )

    pending_details = models.JSONField(
        blank=True,
        null=True,
        verbose_name=_("pending award details"),
        help_text=_(
            "AwardDetails that have been submitted to the remote portal but "
            "are awaiting human review or confirmation (e.g. an allocation "
            "increase that requires manual approval).  Null when nothing is "
            "pending."
        ),
    )

    pending_since = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name=_("pending since"),
        help_text=_("When the currently pending change was submitted."),
    )

    # ------------------------------------------------------------------
    # Allocation (denormalised for fast access; ledger holds full history)
    # ------------------------------------------------------------------

    current_allocation = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        default=0,
        verbose_name=_("current allocation"),
        help_text=_(
            "Latest confirmed allocation (credits) for this project.  "
            "Updated whenever a RemoteProjectAllocationEntry is confirmed."
        ),
    )

    pending_allocation = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        blank=True,
        null=True,
        verbose_name=_("pending allocation"),
        help_text=_(
            "Allocation value currently under review on the remote portal.  "
            "Null when no allocation change is pending."
        ),
    )

    # ------------------------------------------------------------------
    # Connection tracking
    # ------------------------------------------------------------------

    last_contact_time = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name=_("last contact time"),
        help_text=_(
            "The most recent time the remote portal acknowledged anything "
            "about this project (confirmation, usage report, get_award "
            "response, etc.).  Used to detect connectivity issues and to "
            "trigger a transition to the STALE state."
        ),
    )

    # ------------------------------------------------------------------
    # Award-level extras — set by org managers, merged into AwardDetails
    # before each update_award call.
    # ------------------------------------------------------------------

    notes = models.JSONField(
        default=list,
        blank=True,
        verbose_name=_("notes"),
        help_text=_(
            "Append-only list of timestamped notes {timestamp, author, text}. "
            "Kept in sync with the full merged set from award_details() — "
            "includes both locally-authored notes and any notes added by the "
            "remote portal."
        ),
    )

    earliest_approve = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("earliest approve"),
        help_text=_(
            "Earliest UTC time the remote portal may approve this award. "
            "Gives the sender a window to make corrections before "
            "provisioning begins."
        ),
    )

    membership_control = models.CharField(
        max_length=16,
        null=True,
        blank=True,
        choices=MembershipControlChoices.CHOICES,
        verbose_name=_("membership control"),
        help_text=_(
            "Policy controlling whether the remote portal may "
            "independently modify project membership or roles."
        ),
    )

    allowed_domains = models.JSONField(
        null=True,
        blank=True,
        default=None,
        verbose_name=_("allowed domains"),
        help_text=_(
            "List of email domain glob patterns allowed to join the project "
            "on the remote portal, e.g. ['*.ac.uk']. "
            "None means all domains are allowed."
        ),
    )

    breakdown = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_("allocation breakdown"),
        help_text=_(
            "Free-form allocation breakdown sent to the remote portal, "
            "e.g. {'gpu_hours': '500 GPUHR', 'storage': '5 TB'}."
        ),
    )

    link_award = models.JSONField(
        null=True,
        blank=True,
        verbose_name=_("award link"),
        help_text=_("Link to the award record on the funder's system."),
    )

    link_call = models.JSONField(
        null=True,
        blank=True,
        verbose_name=_("call link"),
        help_text=_("Link to the funding call this award came from."),
    )

    link_project = models.JSONField(
        null=True,
        blank=True,
        verbose_name=_("project page link"),
        help_text=_("Link to the project page on the awarding portal."),
    )

    link_renewal = models.JSONField(
        null=True,
        blank=True,
        verbose_name=_("renewal link"),
        help_text=_("Link to where renewal or more time can be requested."),
    )

    # Current live RemoteAllocation handle.  Set to null when the resource
    # is deleted; the RemoteProject record is retained.
    remote_allocation = models.OneToOneField(
        to=RemoteAllocation,
        on_delete=models.SET_NULL,
        related_name="remote_project",
        blank=True,
        null=True,
        verbose_name=_("remote allocation"),
        help_text=_("The current RemoteAllocation that drives this project."),
    )

    # Denormalised FK for fast lookup of the currently attached local project.
    # Updated whenever the award is moved; full history is in
    # RemoteProjectAttachment.
    current_project = models.ForeignKey(
        to=structure_models.Project,
        on_delete=models.SET_NULL,
        related_name="current_remote_projects",
        blank=True,
        null=True,
        verbose_name=_("current project"),
        help_text=_("The local Waldur project currently holding this award."),
    )

    # ------------------------------------------------------------------
    # Timestamps (created / modified via model_utils)
    # ------------------------------------------------------------------

    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(auto_now=True)

    # ------------------------------------------------------------------
    # Convenience properties
    # ------------------------------------------------------------------

    @property
    def has_pending_change(self) -> bool:
        return self.pending_details is not None or self.pending_allocation is not None

    def get_destination(self) -> openportal.Destination:
        return openportal.Destination(self.destination)

    def get_identifier(self) -> openportal.ProjectIdentifier:
        return openportal.ProjectIdentifier(self.identifier)

    def _load_award_details(self, field) -> "openportal.AwardDetails | None":
        if field is None:
            return None
        if isinstance(field, str):
            return openportal.AwardDetails.from_json(field)
        return openportal.AwardDetails.from_json(json.dumps(field))

    def get_last_sent_details(self) -> "openportal.AwardDetails | None":
        return self._load_award_details(self.last_sent_details)

    def get_pending_details(self) -> "openportal.AwardDetails | None":
        return self._load_award_details(self.pending_details)

    def get_last_confirmed_details(self) -> "openportal.AwardDetails | None":
        return self._load_award_details(self.last_confirmed_details)

    def award_details(self) -> "openportal.AwardDetails":
        """
        Compute the best current view of the award by merging last_sent_details
        (our intent — authoritative for locally-authored fields and membership)
        with last_confirmed_details (remote ground truth — authoritative for
        project_link), then layering in the current award-level extras stored
        on this model.

        Merge rules:
        - Locally-authored fields (name, description, dates, template, key,
          allocation, membership_control, allowed_domains): last_sent wins.
        - Notes and breakdown: union from both, notes sorted chronologically.
        - project_link: last_confirmed wins (remote portal owns its own URL),
          unless link_project is explicitly set on this RemoteProject.
        - Members: last_sent is always authoritative — the local portal's team
          membership is the sole source of truth and is never augmented from
          last_confirmed.
        - Extras (link_*, earliest_approve, membership_control,
          allowed_domains, breakdown): current model fields win — they may be
          newer than the last send.

        Always returns a non-None AwardDetails.  If no snapshot exists yet,
        returns the extras only so callers always receive a valid base.
        """

        sent = self.get_last_sent_details()
        confirmed = self.get_last_confirmed_details()

        extras = self.get_extras()
        extras_obj = openportal.AwardDetails.from_json(json.dumps(extras))

        if sent is None and confirmed is None:
            return extras_obj

        if sent is None:
            sent = openportal.AwardDetails("{}")

        if confirmed is None:
            result = sent
        else:
            # confirmed.merge(sent) → sent's locally-authored fields take
            # precedence; notes and breakdown are unioned.
            result = confirmed.merge(sent)

            # Explicitly enforce local membership and membership_control —
            # merge() may blend these, but last_sent is always authoritative.
            result.members = sent.members

        # Layer in current extras — these may be newer than the last send.
        # Notes are unioned (merge deduplicates); other fields overwrite.
        if extras:
            result = result.merge(extras_obj)

            # explicitly control these terms locally
            result.membership_control = extras_obj.membership_control or None
            result.earliest_approve = extras_obj.earliest_approve or None
            result.call = extras_obj.call or None
            result.award = extras_obj.award or None
            result.renewal = extras_obj.renewal or None

            # allowed_domains cannot go through the attribute setter: it
            # normalises an empty list to None, which would turn "nothing
            # allowed" into "no restriction at all".  merge() does preserve an
            # empty list, but only onto a field that is unset, so clear the
            # field first and then merge the local value back in.
            # Still true as of openportal 0.91.0; from_json and merge keep the
            # empty list, only assignment drops it.  See
            # docs/guides/how-to-reconcile-a-fork.md, section 7.
            result.allowed_domains = None
            if self.allowed_domains is not None:
                result = result.merge(
                    openportal.AwardDetails(
                        json.dumps({"allowed_domains": self.allowed_domains})
                    )
                )

        # Remote portal always owns its project URL — restore after extras
        # so that link_project cannot silently override a confirmed value.
        if confirmed is not None and confirmed.project_link is not None:
            result.project_link = confirmed.project_link

        return result

    def get_extras(self) -> dict:
        """
        Return a dict compatible with AwardDetails JSON representing all
        manually-set award-level extras stored on this RemoteProject.
        Called by award_details() to layer current model state on top of
        the snapshot merge.
        """
        extras: dict = {}

        if self.link_award:
            extras["award"] = self.link_award
        if self.link_call:
            extras["call"] = self.link_call
        if self.link_renewal:
            extras["renewal"] = self.link_renewal
        if self.notes:
            extras["notes"] = self.notes

        from django.utils import timezone as tz

        if self.earliest_approve is not None and self.earliest_approve > tz.now():
            extras["earliest_approve"] = self.earliest_approve.isoformat()

        if self.membership_control:
            extras["membership_control"] = self.membership_control
        # None and [] mean different things here — None is "no restriction",
        # [] is "nothing allowed" — so an empty list has to be forwarded
        # rather than treated as unset.
        if self.allowed_domains is not None:
            extras["allowed_domains"] = self.allowed_domains
        if self.breakdown:
            extras["breakdown"] = self.breakdown

        return extras

    def ensure_not_erred(self):
        """
        If the project is in ERROR state, reset it to PENDING so that
        subsequent writes (notes, links, etc.) are not blocked by the
        ERRED allocation state guard.  Does nothing when not in ERROR.
        """
        if self.state == RemoteProjectState.ERROR:
            self.reset_to_pending()

    def reset_to_pending(self):
        """
        Clear a rejection error and return to PENDING state, allowing
        the operator to make further changes or trigger a resend.

        Sets RemoteProject state=PENDING and clears error_message.
        Sets remote_allocation state=OK and clears its error_message,
        so the state guard in update_allocated_project will pass.
        """
        from waldur_core.core.enums import CoreStates

        self.state = RemoteProjectState.PENDING
        self.error_message = ""
        self.save(update_fields=["state", "error_message", "modified"])

        alloc = self.remote_allocation
        if alloc is not None:
            alloc.state = CoreStates.OK
            alloc.error_message = ""
            alloc.save(update_fields=["state", "error_message", "modified"])

        RemoteProjectAuditEntry.objects.create(
            remote_project=self,
            event_type=RemoteProjectAuditEventType.STATE_CHANGED,
            note="Reset to pending by operator.",
        )

    def record_pending(self, sent_details_json=None):
        """
        Transition to PENDING: details have been sent but confirmation
        has not yet been received (e.g. awaiting human review).
        """
        from waldur_openportal import remote_project_service

        remote_project_service.record_award_attempted(self, sent_details_json or {})

    def record_rejected(self, error_message, details_json=None):
        """
        Transition to ERROR: the remote portal rejected the award.

        Delegates to record_award_update_rejected when the project is
        already ACTIVE (an update was rejected), otherwise to
        record_award_rejected (a create was rejected).  Both functions
        also update remote_allocation to ERRED.
        """
        from waldur_openportal import remote_project_service

        if self.state == RemoteProjectState.ACTIVE:
            remote_project_service.record_award_update_rejected(self, error_message)
        else:
            remote_project_service.record_award_rejected(
                self, details_json, error_message
            )

    def record_accepted(self, sent_details_json=None, confirmed_details_json=None):
        """
        Transition to ACTIVE: the remote portal confirmed the award.

        Delegates to record_award_update_confirmed when the project has
        previously been confirmed (last_confirmed_details is set),
        otherwise to record_award_created (initial creation confirmed).
        Both functions also update remote_allocation to OK.
        """
        from waldur_openportal import remote_project_service

        if self.last_confirmed_details is not None:
            remote_project_service.record_award_update_confirmed(
                self, sent_details_json, confirmed_details_json
            )
        else:
            remote_project_service.record_award_created(
                self, sent_details_json, confirmed_details_json
            )

    def __str__(self) -> str:
        return (
            f"RemoteProject [{self.identifier} via {self.destination}] ({self.state})"
        )


class RemoteProjectAttachment(models.Model):
    """
    Records each period during which a local Waldur Project held the award
    represented by a RemoteProject.

    When an award is moved to a new local project:
      1. Set detached_at on the current (open) attachment.
      2. Create a new RemoteProjectAttachment for the new project.
      3. Update RemoteProject.current_project.

    The open attachment (detached_at=None) always matches
    RemoteProject.current_project.
    """

    remote_project = models.ForeignKey(
        to=RemoteProject,
        on_delete=models.CASCADE,
        related_name="attachments",
        verbose_name=_("remote project"),
    )

    project = models.ForeignKey(
        to=structure_models.Project,
        on_delete=models.SET_NULL,
        null=True,
        related_name="remote_project_attachments",
        verbose_name=_("project"),
    )

    attached_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name=_("attached at"),
    )

    detached_at = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name=_("detached at"),
        help_text=_("Null while this is the current attachment."),
    )

    note = models.TextField(
        blank=True,
        verbose_name=_("note"),
        help_text=_("Optional comment recorded when the award was attached or moved."),
    )

    class Meta:
        ordering = ["-attached_at", "id"]
        verbose_name = _("Remote Project Attachment")
        verbose_name_plural = _("Remote Project Attachments")

    def __str__(self) -> str:
        status = "current" if self.detached_at is None else f"until {self.detached_at}"
        return f"{self.remote_project} → {self.project} ({status})"


class RemoteProjectAllocationEntry(models.Model):
    """
    Allocation ledger for a RemoteProject.

    Each row records one allocation change — the new total allocation value,
    what it was before, which local project/award triggered it, and when
    the remote portal confirmed it.

    Allocation values are replacement semantics (not additive): if the
    allocation is set to 100 then 150 then 200, the project has 200 credits.
    However, linking each entry to its source attachment lets you reconstruct
    how much each award contributed over its lifetime, which is needed to
    calculate carry-over credits when an award is moved.

    Usage example (award move):
        spend_under_old_award = <from usage reports>
        last_allocation_under_old_award = (
            RemoteProjectAllocationEntry.objects
            .filter(remote_project=rp, attachment=old_attachment, confirmed_at__isnull=False)
            .order_by("-submitted_at")
            .first()
            .allocation
        )
        remaining = last_allocation_under_old_award - spend_under_old_award
        new_total = remaining + new_credits_from_new_award  # or just new_credits if clean break
    """

    remote_project = models.ForeignKey(
        to=RemoteProject,
        on_delete=models.CASCADE,
        related_name="allocation_history",
        verbose_name=_("remote project"),
    )

    # New total allocation value (replacement, not delta)
    allocation = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        verbose_name=_("allocation"),
        help_text=_("New total allocation (credits) after this change."),
    )

    previous_allocation = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        blank=True,
        null=True,
        verbose_name=_("previous allocation"),
        help_text=_("Total allocation before this change.  Null for the first entry."),
    )

    # Which local award period this allocation change belongs to
    attachment = models.ForeignKey(
        to=RemoteProjectAttachment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="allocation_entries",
        verbose_name=_("attachment"),
        help_text=_("The award attachment period during which this change was made."),
    )

    # Which local project triggered this change
    source_project = models.ForeignKey(
        to=structure_models.Project,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="remote_allocation_entries",
        verbose_name=_("source project"),
        help_text=_("The local project/award that triggered this allocation change."),
    )

    submitted_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name=_("submitted at"),
    )

    confirmed_at = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name=_("confirmed at"),
        help_text=_(
            "When the remote portal confirmed this allocation.  Null if pending."
        ),
    )

    note = models.TextField(
        blank=True,
        verbose_name=_("note"),
        help_text=_(
            "Optional comment, e.g. 'carrying over 20 unused credits from previous award'."
        ),
    )

    class Meta:
        ordering = ["-submitted_at", "id"]
        verbose_name = _("Remote Project Allocation Entry")
        verbose_name_plural = _("Remote Project Allocation Entries")

    @property
    def is_confirmed(self) -> bool:
        return self.confirmed_at is not None

    @property
    def delta(self) -> object:
        """Change in allocation.  Positive = increase, negative = decrease."""
        if self.previous_allocation is None:
            return self.allocation
        return self.allocation - self.previous_allocation

    def __str__(self) -> str:
        status = "confirmed" if self.is_confirmed else "pending"
        return (
            f"{self.remote_project} allocation {self.previous_allocation} → "
            f"{self.allocation} ({status})"
        )


class RemoteProjectAuditEventType(models.TextChoices):
    """Event types for RemoteProjectAuditEntry."""

    # Award lifecycle on the remote portal
    AWARD_ATTEMPTED = (
        "award_attempted",
        _("Award creation attempted but not yet confirmed (pending approval or error)"),
    )
    AWARD_REJECTED = (
        "award_rejected",
        _("Award creation explicitly rejected by remote portal"),
    )
    AWARD_CREATED = "award_created", _("Award created (create_award sent)")
    AWARD_UPDATED = "award_updated", _("Award updated (update_award sent)")
    AWARD_UPDATE_CONFIRMED = (
        "award_update_confirmed",
        _("Award update confirmed by remote portal"),
    )
    AWARD_UPDATE_REJECTED = (
        "award_update_rejected",
        _("Award update rejected by remote portal"),
    )
    # State transitions
    STATE_CHANGED = "state_changed", _("State changed")

    # Resource lifecycle
    RESOURCE_DELETED = "resource_deleted", _("Local resource deleted")


class RemoteProjectAuditEntry(models.Model):
    """
    Full audit log for a RemoteProject.

    Every significant event — award lifecycle, allocation changes, project
    attachment moves, state transitions, resource deletion/restoration — is
    recorded here.  This is the single timeline of everything that happened
    to this RemoteProject.

    For allocation-specific financial history, see RemoteProjectAllocationEntry.
    The two models are linked via the allocation_entry FK so that the audit
    timeline remains complete while detailed financial data stays queryable
    in its own table.
    """

    remote_project = models.ForeignKey(
        to=RemoteProject,
        on_delete=models.CASCADE,
        related_name="audit_log",
        verbose_name=_("remote project"),
    )

    timestamp = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        verbose_name=_("timestamp"),
    )

    event_type = models.CharField(
        max_length=32,
        choices=RemoteProjectAuditEventType.choices,
        db_index=True,
        verbose_name=_("event type"),
    )

    # AwardDetails snapshots — what changed
    previous_details = models.JSONField(
        blank=True,
        null=True,
        verbose_name=_("previous award details"),
        help_text=_("AwardDetails before this event.  Null for creation events."),
    )

    new_details = models.JSONField(
        blank=True,
        null=True,
        verbose_name=_("new award details"),
        help_text=_(
            "AwardDetails after this event, or the details being requested.  "
            "Null for events that do not change the details."
        ),
    )

    # Link to the allocation ledger entry when this event concerns an
    # allocation change.  Null for non-allocation events.
    allocation_entry = models.ForeignKey(
        to=RemoteProjectAllocationEntry,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_entries",
        verbose_name=_("allocation entry"),
        help_text=_(
            "The allocation ledger entry associated with this event, if applicable."
        ),
    )

    # Who triggered this event (null for automated/system events)
    performed_by = models.ForeignKey(
        to=core_models.User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("performed by"),
        help_text=_("The user who triggered this event, if it was a human action."),
    )

    # Raw response from the remote portal (useful for debugging rejections)
    remote_response = models.JSONField(
        blank=True,
        null=True,
        verbose_name=_("remote response"),
        help_text=_("Raw response received from the remote portal, if applicable."),
    )

    note = models.TextField(
        blank=True,
        verbose_name=_("note"),
        help_text=_("Optional free-text comment about this event."),
    )

    class Meta:
        ordering = ["-timestamp", "id"]
        verbose_name = _("Remote Project Audit Entry")
        verbose_name_plural = _("Remote Project Audit Entries")

    def __str__(self) -> str:
        return f"{self.remote_project} — {self.event_type} at {self.timestamp}"

    def __repr__(self) -> str:
        return self.__str__()
