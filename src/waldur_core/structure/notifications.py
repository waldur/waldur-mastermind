from typing import Any, TypeVar

from pydantic import BaseModel, Field, model_validator


class NotificationTemplate(BaseModel):
    """Defines a single template file associated with a notification."""

    path: str
    name: str


# A generic TypeVar that must be a Pydantic BaseModel.
# This allows us to create a Notification class that is typed with its specific context model.
ContextType = TypeVar("ContextType", bound=BaseModel)


class Notification[ContextType: BaseModel](BaseModel):
    """
    Represents a single notification type, including its key, description,
    and a strongly-typed schema for its context variables.
    """

    key: str
    description: str
    context_model: type[ContextType] = Field(
        ...,
        description="The Pydantic model that defines the schema for the notification's context.",
    )
    templates: list[NotificationTemplate] | None = None

    @model_validator(mode="after")
    def set_default_templates(self) -> "Notification":
        """
        Most notifications have standard templates. This validator sets them by default
        if they are not provided explicitly, correctly mapping keys to file paths.
        Example: 'users.invitation_approved' -> 'users/invitation_approved_subject.txt'
        """
        if self.templates is None:
            # Replace the first dot with a slash to form the base path
            path_base = self.key.replace(".", "/", 1)
            paths = [
                f"{path_base}_subject.txt",
                f"{path_base}_message.txt",
                f"{path_base}_message.html",
            ]
            self.templates = [NotificationTemplate(path=p, name=p) for p in paths]
        return self


NOTIFICATIONS = dict()


class NotificationSectionMetaclass(type):
    """
    A metaclass that automatically discovers Notification instances within a class
    and registers them in the global NOTIFICATIONS dictionary.
    """

    def __new__(cls, name, bases, attrs):
        if "Meta" in attrs:
            section_key = attrs["Meta"].key
            section_list = []
            NOTIFICATIONS[section_key] = section_list

            for _, notification in attrs.items():
                if isinstance(notification, Notification):
                    # For each notification, we store its essential info, including
                    # a full JSON schema of its context model for introspection.
                    section_list.append(
                        {
                            "path": notification.key,
                            "description": notification.description,
                            "templates": [
                                tpl.model_dump() for tpl in notification.templates
                            ]
                            if notification.templates
                            else [],
                            "context_schema": notification.context_model.model_json_schema(),
                        }
                    )

        return super().__new__(cls, name, bases, attrs)


class NotificationSection(metaclass=NotificationSectionMetaclass):
    """Base class for organizing notifications into sections."""

    pass


class EmptyContext(BaseModel):
    """A reusable context model for notifications that require no variables."""

    pass


class ProfileChangesOperatorContext(BaseModel):
    user: Any = Field(
        description="The User model instance whose profile has been updated. Provides access to fields like `user.full_name` and `user.id`."
    )
    fields: list[dict[str, str]] = Field(
        description="A list of dictionaries, where each dictionary represents a changed field and contains string keys: `name`, `old_value`, and `new_value`."
    )
    organizations: Any = Field(
        description="A queryset of Customer model instances where the user is an owner. Can be iterated over in the template to access `o.name` and `o.abbreviation`."
    )


class ChangeEmailRequestContext(BaseModel):
    request: Any = Field(
        description="The ChangeEmailRequest model instance. Provides access to `request.user.email` and the new `request.email`."
    )
    link: str = Field(
        description="A string URL for the user to click to confirm the email change."
    )


class StructureRoleGrantedContext(BaseModel):
    permission: Any = Field(
        description="The Permission model instance (e.g., ProjectPermission). Provides `permission.role`."
    )
    structure: Any = Field(
        description="The model instance where the role was granted (e.g., a Project or Customer object)."
    )


class ProjectEndDateChangeRequestContext(BaseModel):
    project_end_date_change_request: Any = Field(
        description="The ProjectEndDateChangeRequest instance. Provides project_end_date_change_request.project.name, requested_end_date, created_by.full_name."
    )
    project_url: str = Field(description="A URL to the project's page.")


class StructureSection(NotificationSection):
    class Meta:
        key = "structure"

    notification_project_end_date_change_request_created = Notification(
        key="notification_project_end_date_change_request_created",
        description="Notifies organization owners when a project member requests to change project end date.",
        context_model=ProjectEndDateChangeRequestContext,
    )
    notification_project_end_date_change_request_approved = Notification(
        key="notification_project_end_date_change_request_approved",
        description="Notifies the requester when their project end date change request is approved.",
        context_model=ProjectEndDateChangeRequestContext,
    )
    notification_project_end_date_change_request_rejected = Notification(
        key="notification_project_end_date_change_request_rejected",
        description="Notifies the requester when their project end date change request is rejected.",
        context_model=ProjectEndDateChangeRequestContext,
    )
    change_email_request = Notification(
        key="change_email_request",
        description="A notification sent out when an email change is requested. Recipient is the old email address.",
        context_model=ChangeEmailRequestContext,
    )

    notifications_profile_changes_operator = Notification(
        key="notifications_profile_changes_operator",
        description="A notification sent to Waldur operators when a user's profile is updated.",
        context_model=ProfileChangesOperatorContext,
    )

    structure_role_granted = Notification(
        key="structure_role_granted",
        description="A notification sent out when a role is granted. The recipient is the user who received the role.",
        context_model=StructureRoleGrantedContext,
    )

    project_digest = Notification(
        key="project_digest",
        description="Periodic project summary digest sent to project members.",
        context_model=EmptyContext,
    )


class BaseInvitationContext(BaseModel):
    name: str = Field(
        description="The name of the structure (Project or Organization) the user is invited to."
    )
    type: str = Field(
        description="The type of the structure, either 'project' or 'organization'."
    )
    role: str = Field(description="The name of the role being granted.")
    sender: str = Field(
        description="The full name of the user who created the invitation."
    )


class InvitationCreatedContext(BaseInvitationContext):
    link: str = Field(
        description="The unique URL for the user to accept the invitation."
    )
    extra_invitation_text: str = Field(
        description="Any additional text provided by the inviter."
    )
    reminder: bool = Field(
        description="A boolean flag, set to `True` if this is a reminder for a pending invitation."
    )
    scope_link: str = Field(description="The URL of the scope the user is invited to.")
    invitation: Any = Field(
        description="The GroupInvitation instance. Used to access `invitation.get_expiration_time`."
    )


class CallInvitationCreatedContext(InvitationCreatedContext):
    organizer_name: str = Field(
        description="The name of the organization managing the call."
    )


class ProposalInvitationCreatedContext(InvitationCreatedContext):
    call_name: str = Field(
        description="The name of the call the proposal is submitted to."
    )
    round_cutoff_time: Any = Field(
        description="The submission deadline of the round the proposal belongs to."
    )


class InvitationRequestedContext(BaseInvitationContext):
    invitation: Any = Field(
        description="The GroupInvitation instance. Used to access details like `invitation.civil_number`, `invitation.email`, etc."
    )
    approve_link: str = Field(
        description="A unique URL for staff to approve the invitation."
    )
    reject_link: str = Field(
        description="A unique URL for staff to reject the invitation."
    )


class InvitationApprovedContext(BaseInvitationContext):
    username: str = Field(description="The generated username for the new user.")
    password: str = Field(
        description="The generated temporary password for the new user."
    )
    link: str = Field(
        description="The unique URL for the new user to access their account."
    )


class InvitationRejectedContext(BaseInvitationContext):
    invitation: Any = Field(
        description="The GroupInvitation instance. Used to access `invitation.full_name`."
    )


class InvitationExpiredContext(BaseModel):
    invitation: Any = Field(
        description="The GroupInvitation instance. Used to access `invitation.email` and `invitation.get_expiration_time`."
    )


class PermissionRequestSubmittedContext(BaseModel):
    permission_request: Any = Field(
        description="The PermissionRequest model instance. Provides `permission_request.created_by` and `permission_request.invitation`."
    )
    requests_link: str = Field(
        description="A URL to the page for reviewing permission requests."
    )


class PermissionRequestRejectedContext(BaseModel):
    permission_request: Any = Field(
        description="The PermissionRequest model instance. Provides `permission_request.created_by`, `permission_request.invitation`, `permission_request.review_comment`, and `permission_request.reviewed_by`."
    )


class UserSection(NotificationSection):
    class Meta:
        key = "users"

    invitation_approved = Notification(
        key="invitation_approved",
        description="Sent to a new user after their invitation is approved and a new account is created for them.",
        context_model=InvitationApprovedContext,
    )
    invitation_created = Notification(
        key="invitation_created",
        description="Sent to an invited user so they can accept the invitation.",
        context_model=InvitationCreatedContext,
    )
    call_invitation_created = Notification(
        key="call_invitation_created",
        description="Sent to a user invited to a call for proposals so they can accept the invitation.",
        context_model=CallInvitationCreatedContext,
    )
    proposal_invitation_created = Notification(
        key="proposal_invitation_created",
        description="Sent to a user invited to a proposal team so they can accept the invitation.",
        context_model=ProposalInvitationCreatedContext,
    )
    invitation_expired = Notification(
        key="invitation_expired",
        description="Sent to the invitation creator to inform them that an invitation has expired.",
        context_model=InvitationExpiredContext,
    )
    invitation_rejected = Notification(
        key="invitation_rejected",
        description="Sent to the invitation creator to inform them that their invitation has been rejected.",
        context_model=InvitationRejectedContext,
    )
    invitation_requested = Notification(
        key="invitation_requested",
        description="Sent to staff users so they can approve or reject a pending invitation.",
        context_model=InvitationRequestedContext,
    )
    permission_request_submitted = Notification(
        key="permission_request_submitted",
        description="Sent about a submitted permission request to the organization owners and managers who can approve it, and to the organization's contact and notification emails. Falls back to staff when none of these are available.",
        context_model=PermissionRequestSubmittedContext,
    )
    permission_request_rejected = Notification(
        key="permission_request_rejected",
        description="Sent to the user who submitted a permission request to inform them that their request has been rejected.",
        context_model=PermissionRequestRejectedContext,
    )


class BookingNotificationContext(BaseModel):
    resources: list[Any] = Field(
        description="A list of upcoming Booking objects for the user. Each object provides a `name` attribute."
    )


class BookingSection(NotificationSection):
    class Meta:
        key = "booking"

    notification = Notification(
        key="notification",
        description="Sent to users to notify them about their upcoming bookings.",
        context_model=BookingNotificationContext,
    )


class UpcomingEndsNotificationContext(BaseModel):
    organization_name: str = Field(
        description="The name of the organization whose contract is ending."
    )
    end: str = Field(description="The end date of the contract.")
    contract_number: str | None = Field(
        default=None, description="The contract number, if available."
    )


class InvoiceNotificationContext(BaseModel):
    month: int = Field(description="The month of the invoice as an integer.")
    year: int = Field(description="The year of the invoice as an integer.")
    customer: str = Field(description="The name of the customer the invoice is for.")
    link: str = Field(description="A URL to view the invoice in the portal.")


class InvoiceSection(NotificationSection):
    class Meta:
        key = "invoices"

    notification = Notification(
        key="notification",
        description="Sent to organization owners with a new invoice. Includes the invoice as an HTML attachment.",
        context_model=InvoiceNotificationContext,
    )
    upcoming_ends_notification = Notification(
        key="upcoming_ends_notification",
        description="Notifies organization owners about an upcoming fixed-price contract ending.",
        context_model=UpcomingEndsNotificationContext,
    )


class ResourceNameContext(BaseModel):
    resource_name: str = Field(description="The name of the affected resource.")


class BaseOrderContext(BaseModel):
    order: Any = Field(
        description="The Order model instance. Provides access to `order.created_by`, `order.resource.name`, etc."
    )
    site_name: str = Field(description="The name of the site from settings.")


class NotifyConsumerAboutPendingOrderContext(BaseOrderContext):
    order_link: str = Field(description="A URL to the order details page.")


class NotifyProviderAboutPendingOrderContext(BaseOrderContext):
    order_url: str = Field(
        description="A URL to the order details page for the provider."
    )


class NewOrderNotificationContext(NotifyProviderAboutPendingOrderContext):
    order_type: str = Field(
        description="The display name of the order type (e.g., 'create', 'update', 'terminate')."
    )


class OrderRejectedContext(BaseOrderContext):
    link: str = Field(description="A URL to the rejected order's details page.")
    order_type: str = Field(
        description="The display name of the order type (e.g., 'create', 'update', 'terminate')."
    )


class ResourceUpdateSucceededContext(BaseModel):
    resource_name: str = Field(description="The name of the updated resource.")
    order_user: str = Field(
        description="The full name of the user who initiated the update order."
    )
    resource_old_plan: str | None = Field(
        default=None, description="The name of the resource's previous plan."
    )
    resource_plan: str | None = Field(
        default=None, description="The name of the resource's new plan."
    )
    support_email: str | None = Field(
        default=None, description="The site's support email address from settings."
    )
    support_phone: str | None = Field(
        default=None, description="The site's support phone number from settings."
    )


class ResourceUpdateLimitsSucceededContext(BaseModel):
    resource_name: str = Field(description="The name of the updated resource.")
    order_user: str = Field(
        description="The full name of the user who initiated the update order."
    )
    resource_old_limits: str = Field(
        description="A formatted, human-readable list of the resource's previous limits."
    )
    resource_limits: str = Field(
        description="A formatted, human-readable list of the resource's new limits."
    )
    support_email: str | None = Field(
        default=None, description="The site's support email address from settings."
    )
    support_phone: str | None = Field(
        default=None, description="The site's support phone number from settings."
    )


class UsagesNotificationContext(BaseModel):
    resources: list[Any] = Field(
        description="A list of resource objects that are missing usage reports. The list is regrouped by offering in the template."
    )
    public_resources_url: str = Field(
        description="A URL to the public resources page where usage can be submitted."
    )


class StaleResourcesContext(BaseModel):
    resources: list[dict[str, Any]] = Field(
        description="A list of dictionaries. Each dictionary contains `resource` (the Resource model instance) and `resource_url` (a string URL to its details page)."
    )


class ResourceTerminationScheduledContext(BaseModel):
    resource: Any = Field(
        description="The Resource model instance being terminated. Provides `resource.name` and `resource.end_date`."
    )
    user: Any = Field(
        description="The User model instance who scheduled the termination. Provides `user.full_name`."
    )
    resource_url: str = Field(description="A URL to the resource details page.")


class ProjectEndingContext(BaseModel):
    projects: list[Any] = Field(
        description="A list of Project model instances that are ending. Each project is annotated with a `.url` attribute."
    )
    user: Any = Field(
        description="The User model instance receiving the notification. Provides `user.full_name`."
    )
    end_date: Any = Field(description="The date when the projects will end.")
    count_projects: int = Field(
        description="The number of projects in the `projects` list."
    )
    delta: int = Field(description="The number of days until the project ends.")


class ResourceEndingContext(BaseModel):
    resource: Any = Field(
        description="The Resource model instance. Provides `resource.name`."
    )
    user: Any = Field(
        description="The User model instance receiving the notification. Provides `user.full_name`."
    )
    resource_url: str = Field(description="A URL to the resource details page.")
    delta: int = Field(description="The number of days until the resource's end date.")


class ToSConsentRequiredContext(BaseModel):
    user: Any = Field(
        description="The User model instance who needs to provide consent."
    )
    offering: Any = Field(
        description="The Offering model instance. Provides offering.name."
    )
    terms_of_service_link: str = Field(
        description="Direct link to the Terms of Service document."
    )
    tos_management_url: str = Field(
        description="URL to the ToS management page where user can manage all consents."
    )
    version: str = Field(description="Version of the ToS requiring consent.")
    site_name: str = Field(description="Name of the site from settings.")


class ToSReconsentRequiredContext(BaseModel):
    user: Any = Field(description="The User model instance whose consent expired.")
    offering: Any = Field(
        description="The Offering model instance. Provides offering.name."
    )
    terms_of_service_link: str = Field(
        description="Direct link to the updated Terms of Service."
    )
    tos_management_url: str = Field(
        description="URL to the ToS management page where user can consent or revoke consent."
    )
    old_version: str = Field(description="Previous ToS version.")
    new_version: str = Field(description="New ToS version requiring re-consent.")
    site_name: str = Field(description="Name of the site from settings.")


class ResourceLimitChangeRequestContext(BaseModel):
    resource_limit_change_request: Any = Field(
        description="The ResourceLimitChangeRequest instance. Provides "
        "resource_limit_change_request.resource.name, requested_limits, "
        "created_by.full_name."
    )
    resource_url: str = Field(description="A URL to the resource's page.")


class MarketplaceSection(NotificationSection):
    class Meta:
        key = "marketplace"

    marketplace_resource_create_failed = Notification(
        key="marketplace_resource_create_failed",
        description="A notification of a failed resource creation",
        context_model=ResourceNameContext,
    )
    marketplace_resource_create_succeeded = Notification(
        key="marketplace_resource_create_succeeded",
        description="A notification of a successful resource creation",
        context_model=ResourceNameContext,
    )
    marketplace_resource_terminate_failed = Notification(
        key="marketplace_resource_terminate_failed",
        description="A notification of a failed resource termination",
        context_model=ResourceNameContext,
    )
    marketplace_resource_terminate_succeeded = Notification(
        key="marketplace_resource_terminate_succeeded",
        description="A notification of a successful resource termination",
        context_model=ResourceNameContext,
    )
    marketplace_resource_termination_scheduled = Notification(
        key="marketplace_resource_termination_scheduled",
        description="Notifies project admins/managers that a resource termination was scheduled.",
        context_model=ResourceTerminationScheduledContext,
    )
    marketplace_resource_termination_scheduled_staff = Notification(
        key="marketplace_resource_termination_scheduled_staff",
        description="A notification of a resource termination. The recipients are project administrators and managers.",
        context_model=ResourceTerminationScheduledContext,
    )
    marketplace_resource_update_failed = Notification(
        key="marketplace_resource_update_failed",
        description="A notification of failed resource update",
        context_model=ResourceNameContext,
    )
    marketplace_resource_update_limits_failed = Notification(
        key="marketplace_resource_update_limits_failed",
        description="A notification of failed resource limits update",
        context_model=ResourceNameContext,
    )
    marketplace_resource_update_limits_succeeded = Notification(
        key="marketplace_resource_update_limits_succeeded",
        description="A notification of a successful resource limit update. The recipients are all the users in the project.",
        context_model=ResourceUpdateLimitsSucceededContext,
    )
    marketplace_resource_update_succeeded = Notification(
        key="marketplace_resource_update_succeeded",
        description="A notification of a successful resource plan update. The recipients are all the users in the project.",
        context_model=ResourceUpdateSucceededContext,
    )
    notification_about_project_ending = Notification(
        key="notification_about_project_ending",
        description="Notifies project users about a resource that is nearing its end date.",
        context_model=ProjectEndingContext,
    )
    notification_about_resource_ending = Notification(
        key="notification_about_resource_ending",
        description="A notification about resource ending. The recipients are project managers and customer owners.",
        context_model=ResourceEndingContext,
    )
    notification_about_stale_resources = Notification(
        key="notification_about_stale_resources",
        description="Notifies organization owners about active resources that have not generated costs recently.",
        context_model=StaleResourcesContext,
    )
    notification_to_user_that_order_been_rejected = Notification(
        key="notification_to_user_that_order_been_rejected",
        description="Notification to user whose order been rejected.",
        context_model=OrderRejectedContext,
    )
    notification_usages = Notification(
        key="notification_usages",
        description="A notification about usages. The recipients are organization owners.",
        context_model=UsagesNotificationContext,
    )
    notify_about_new_order = Notification(
        key="notify_about_new_order",
        description="Notifies the recipients configured on the offering about every "
        "new order for it, regardless of whether the order needs approval.",
        context_model=NewOrderNotificationContext,
    )
    notify_consumer_about_pending_order = Notification(
        key="notify_consumer_about_pending_order",
        description="Notifies project members with approval permissions about a pending order.",
        context_model=NotifyConsumerAboutPendingOrderContext,
    )
    notify_provider_about_pending_order = Notification(
        key="notify_provider_about_pending_order",
        description="Notifies service provider owners about a pending order for their offering.",
        context_model=NotifyProviderAboutPendingOrderContext,
    )
    notify_consumer_about_provider_info = Notification(
        key="notify_consumer_about_provider_info",
        description="Notifies the order creator when the provider sends a message on a pending order.",
        context_model=NotifyProviderAboutPendingOrderContext,
    )
    notify_provider_about_consumer_info = Notification(
        key="notify_provider_about_consumer_info",
        description="Notifies the provider when the consumer responds with a message on a pending order.",
        context_model=NotifyProviderAboutPendingOrderContext,
    )
    tos_consent_required = Notification(
        key="tos_consent_required",
        description="Notifies user that ToS consent is required to access a resource.",
        context_model=ToSConsentRequiredContext,
    )
    tos_reconsent_required = Notification(
        key="tos_reconsent_required",
        description="Notifies user that ToS has been updated and re-consent is required.",
        context_model=ToSReconsentRequiredContext,
    )
    notification_resource_limit_change_request_created = Notification(
        key="notification_resource_limit_change_request_created",
        description="Notifies organization owners when a project member requests a resource limit change.",
        context_model=ResourceLimitChangeRequestContext,
    )
    notification_resource_limit_change_request_approved = Notification(
        key="notification_resource_limit_change_request_approved",
        description="Notifies the requester when their resource limit change request is approved.",
        context_model=ResourceLimitChangeRequestContext,
    )
    notification_resource_limit_change_request_rejected = Notification(
        key="notification_resource_limit_change_request_rejected",
        description="Notifies the requester when their resource limit change request is rejected.",
        context_model=ResourceLimitChangeRequestContext,
    )


class PendingProjectUpdatesContext(BaseModel):
    project_update_request: Any = Field(
        description="The ProjectUpdateRequest model instance that is pending. Provides `project_update_request.project.name`."
    )
    project_url: str = Field(description="A URL to the project's page.")


class ProjectDetailsUpdateContext(BaseModel):
    project_url: str = Field(description="A URL to the project's page.")
    new_name: str | None = Field(
        default=None, description="The new project name string."
    )
    old_name: str | None = Field(
        default=None, description="The old project name string."
    )
    new_description: str | None = Field(
        default=None, description="The new project description string."
    )
    old_description: str | None = Field(
        default=None, description="The old project description string."
    )
    new_end_date: Any | None = Field(
        default=None, description="The new project end date."
    )
    old_end_date: Any | None = Field(
        default=None, description="The old project end date."
    )
    new_oecd_fos_2007_code: str | None = Field(default=None)
    old_oecd_fos_2007_code: str | None = Field(default=None)
    new_is_industry: bool | None = Field(default=None)
    old_is_industry: bool | None = Field(default=None)
    reviewed_by: Any = Field(description="The User object who reviewed the request.")


class ProjectCostLimitContext(BaseModel):
    scope_class: str = Field(
        description="The class name of the scope, e.g., 'Project'."
    )
    scope_name: str = Field(
        description="The name of the scope instance (e.g., the project's name)."
    )
    scope_url: str = Field(description="A URL to the scope's page in the portal.")
    limit: float = Field(description="The cost limit that was exceeded.")


class ResourceEndDatePulledContext(BaseModel):
    resource: Any = Field(
        description="The Resource model instance whose end date was updated."
    )
    old_end_date: str = Field(description="The previous end date (as string).")
    new_end_date: str = Field(
        description="The new end date pulled from remote (as string)."
    )
    resource_url: str = Field(description="A URL to the resource's detail page.")
    remote_events: list = Field(
        default_factory=list,
        description="List of recent related events from the remote system.",
    )


class MarketplaceRemoteSection(NotificationSection):
    class Meta:
        key = "marketplace_remote"

    notification_about_pending_project_updates = Notification(
        key="notification_about_pending_project_updates",
        description="A notification about pending project updates. The recipients are customer owners",
        context_model=PendingProjectUpdatesContext,
    )
    notification_about_project_details_update = Notification(
        key="notification_about_project_details_update",
        description="Notifies users about a completed project update request, detailing the changes.",
        context_model=ProjectDetailsUpdateContext,
    )
    resource_end_date_pulled_from_remote = Notification(
        key="resource_end_date_pulled_from_remote",
        description="Notification sent when a resource's end date is automatically updated from the remote allocation system because the local date was in the past.",
        context_model=ResourceEndDatePulledContext,
    )


class PolicySection(NotificationSection):
    class Meta:
        key = "marketplace_policy"

    notification_about_project_cost_exceeded_limit = Notification(
        key="notification_about_project_cost_exceeded_limit",
        description="Notification about project cost exceeded limit. The recipients are all customer owners of the project.",
        context_model=ProjectCostLimitContext,
    )


class IssueGenerationContext(BaseModel):
    issue: Any = Field(
        description="The Issue model instance being created. Provides access to all its fields like `issue.customer`, `issue.project`, `issue.resource`, etc."
    )
    settings: Any = Field(description="Global settings object.")
    config: Any = Field(description="Global config object.")


class BaseIssueContext(BaseModel):
    issue: Any = Field(description="The Issue model instance.")
    issue_url: str = Field(description="A direct URL to the issue's page.")


class CommentAddedContext(BaseIssueContext):
    comment: Any = Field(
        description="The newly created Comment model instance. Provides access to `comment.author.name`."
    )
    description: str = Field(description="The content/text of the new comment.")
    is_system_comment: bool = Field(
        description="A boolean indicating if the comment was auto-generated by an integration rather than a human user."
    )


class CommentUpdatedContext(BaseIssueContext):
    comment: Any = Field(
        description="The Comment model instance that was updated. Provides `comment.author.name`."
    )
    description: str = Field(description="The new, current content of the comment.")
    old_description: str = Field(
        description="The previous content of the comment before it was edited."
    )


class IssueFeedbackContext(BaseIssueContext):
    feedback_links: list[dict[str, str]] = Field(
        description="A list of dictionaries for creating a rating scale. Each dictionary contains a `label` and a unique `link`."
    )


class IssueUpdatedContext(BaseIssueContext):
    site_name: str = Field(description="The name of the platform.")
    changed: dict[str, Any] = Field(
        description="A dictionary containing the fields that were updated, with their old values."
    )
    description: str = Field(description="The new, current description of the issue.")
    old_description: str = Field(
        description="The previous description of the issue before the update."
    )


class ProviderTicketContext(BaseModel):
    issue: Any = Field(
        description="The Issue model instance routed to (or withdrawn from) the provider helpdesk."
    )
    provider_helpdesk: Any = Field(
        description="The ProviderHelpdesk model instance the ticket relates to."
    )


class ProviderCommentContext(BaseModel):
    issue: Any = Field(description="The Issue model instance.")
    comment: Any = Field(
        description="The Comment model instance added by the customer."
    )
    provider_helpdesk: Any = Field(
        description="The ProviderHelpdesk model instance the ticket relates to."
    )


class ProviderEscalationContext(BaseModel):
    issue: Any = Field(description="The parent (operator) Issue that was escalated.")
    child_issue: Any = Field(
        description="The child Issue routed to the provider helpdesk."
    )
    reason: str = Field(description="The escalation reason.")


class SupportSection(NotificationSection):
    class Meta:
        key = "support"

    description = Notification(
        key="description",
        description="A template used for generating the issue description field during issue creation.",
        templates=[NotificationTemplate(path="description.txt", name="description")],
        context_model=IssueGenerationContext,
    )
    notification_comment_added = Notification(
        key="notification_comment_added",
        description="Notification about a new comment in the issue. The recipient is issue caller.",
        context_model=CommentAddedContext,
    )
    notification_comment_updated = Notification(
        key="notification_comment_updated",
        description="Notification about an update in the issue comment. The recipient is issue caller.",
        context_model=CommentUpdatedContext,
    )
    notification_issue_feedback = Notification(
        key="notification_issue_feedback",
        description="Notification about a feedback related to the issue. The recipient is issue caller.",
        context_model=IssueFeedbackContext,
    )
    notification_issue_updated = Notification(
        key="notification_issue_updated",
        description="Notification about an update in the issue. The recipient is issue caller.",
        context_model=IssueUpdatedContext,
    )
    summary = Notification(
        key="summary",
        description="A template used for generating the issue summary field during issue creation.",
        templates=[NotificationTemplate(path="summary.txt", name="summary")],
        context_model=IssueGenerationContext,
    )
    provider_new_ticket = Notification(
        key="provider_new_ticket",
        description="Notify a provider helpdesk about a new ticket routed to them.",
        context_model=ProviderTicketContext,
    )
    provider_ticket_withdrawn = Notification(
        key="provider_ticket_withdrawn",
        description="Notify a provider helpdesk that a ticket previously routed to them was rerouted away.",
        context_model=ProviderTicketContext,
    )
    provider_escalation = Notification(
        key="provider_escalation",
        description="Notify a provider helpdesk that a routed ticket has been escalated.",
        context_model=ProviderEscalationContext,
    )
    provider_email_new_ticket = Notification(
        key="provider_email_new_ticket",
        description="Email a provider a new ticket via the email support backend.",
        context_model=ProviderTicketContext,
    )
    provider_email_comment = Notification(
        key="provider_email_comment",
        description="Email a provider a customer comment via the email support backend.",
        context_model=ProviderCommentContext,
    )


class ProposalStateChangedContext(BaseModel):
    site_name: str = Field(description="Name of the site from settings.")
    new_state: str = Field(
        description="The new state of the proposal (e.g., 'accepted')."
    )
    previous_state: str = Field(description="The previous state of the proposal.")
    update_date: Any = Field(description="The timestamp of the state change.")
    proposal_url: str = Field(description="URL to the proposal details page.")
    project_url: str | None = Field(
        default=None,
        description="URL to the created project if the proposal was accepted.",
    )
    project_name: str | None = Field(
        default=None, description="Name of the created project."
    )
    allocation_date: Any | None = Field(
        default=None, description="The day the granted project starts running."
    )
    duration: int | None = Field(
        default=None,
        description=(
            "How long the granted project runs, in days. None where the grant "
            "does not expire, in which case the line is not rendered."
        ),
    )
    proposal_name: str = Field(description="Name of the proposal.")
    proposal_creator_name: str = Field(
        description="Full name of the proposal's creator."
    )
    call_name: str = Field(description="Name of the call for proposals.")
    rejection_feedback: str | None = Field(
        default=None,
        description="Comments from the manager if the proposal was rejected.",
    )
    allocated_resources: list[dict[str, str]] | None = Field(
        default=None,
        description="A list of dictionaries for created resources if accepted.",
    )
    review_period: int | None = Field(
        default=None,
        description="The expected number of days for the review process.",
    )


class NewProposalSubmittedContext(BaseModel):
    site_name: str = Field(description="Name of the site from settings.")
    proposal_url: str = Field(
        description="URL for the call manager to view the proposal."
    )
    proposal_name: str = Field(description="Name of the submitted proposal.")
    proposal_creator_name: str = Field(
        description="Full name of the proposal's creator."
    )
    call_name: str = Field(description="Name of the call.")
    round_name: str = Field(description="Name of the round.")
    submission_date: Any = Field(description="The date and time of submission.")


class NewReviewSubmittedContext(BaseModel):
    site_name: str = Field(description="Name of the site from settings.")
    review_url: str = Field(description="URL for the call manager to view the review.")
    proposal_name: str = Field(description="Name of the proposal being reviewed.")
    call_name: str = Field(description="Name of the call.")
    reviewer_name: str = Field(description="Full name of the reviewer.")
    submission_date: Any = Field(
        description="The timestamp when the review was submitted."
    )
    review_date: Any = Field(description="The timestamp when the review was submitted.")
    score: float = Field(description="The summary score given by the reviewer.")
    max_score: str = Field(description="The maximum possible score (string '5').")
    submitted_reviews: int = Field(description="Number of reviews already submitted.")
    pending_reviews: int = Field(description="Number of reviews still pending.")
    rejected_reviews: int = Field(description="Number of rejected/cancelled reviews.")


class ReviewRejectedContext(BaseModel):
    site_name: str = Field(description="Name of the site from settings.")
    proposal_name: str = Field(
        description="Name of the proposal for the rejected review."
    )
    call_name: str = Field(description="Name of the call.")
    reviewer_name: str = Field(description="Full name of the reviewer who rejected.")
    assign_date: Any = Field(description="The timestamp when the review was assigned.")
    rejection_date: Any = Field(
        description="The timestamp when the review was rejected."
    )
    create_review_link: str = Field(
        description="A URL for the manager to assign a new review."
    )
    submitted_reviews: int = Field(description="Number of reviews already submitted.")
    pending_reviews: int = Field(description="Number of reviews still pending.")
    rejected_reviews: int = Field(description="Number of rejected/cancelled reviews.")


class ProposalCancelledContext(BaseModel):
    site_name: str = Field(description="Name of the site from settings.")
    proposal_name: str = Field(description="Name of the cancelled proposal.")
    call_name: str = Field(description="Name of the call.")
    cancellation_date: Any = Field(description="The date and time of cancellation.")
    proposal_url: str = Field(description="A URL to the cancelled proposal.")
    proposal_creator_name: str = Field(description="Full name of the proposal creator.")


class ReviewAssignedContext(BaseModel):
    site_name: str = Field(description="Name of the site from settings.")
    reviewer_name: str = Field(description="Full name of the assigned reviewer.")
    call_name: str = Field(description="Name of the call.")
    proposal_name: str = Field(description="Name of the proposal to be reviewed.")
    proposal_creator_name: str = Field(
        description="Full name of the proposal's creator."
    )
    submission_date: Any = Field(description="The date the proposal was submitted.")
    review_deadline: Any = Field(description="The deadline for submitting the review.")
    link_to_reviews_list: str = Field(
        description="A URL to the reviewer's list of assigned reviews."
    )


class ReviewDeadlineApproachingContext(BaseModel):
    site_name: str = Field(description="Name of the site from settings.")
    reviewer_name: str = Field(description="Full name of the reviewer.")
    proposal_name: str = Field(description="Name of the proposal under review.")
    call_name: str = Field(description="Name of the call.")
    review_deadline: Any = Field(description="The review due date and time.")
    time_remaining_days: int = Field(
        description="Whole number of days remaining until the review deadline."
    )
    review_url: str = Field(description="A URL where the reviewer can continue review.")


class ProposalDecisionForReviewerContext(BaseModel):
    site_name: str = Field(description="Name of the site from settings.")
    proposal_state: str = Field(
        description="The final state of the proposal (e.g., 'approved', 'rejected')."
    )
    proposal_url: str = Field(description="A URL to the proposal.")
    proposal_name: str = Field(description="Name of the proposal.")
    call_name: str = Field(description="Name of the call.")
    decision_date: Any = Field(description="The date the decision was made.")
    rejection_reason: str | None = Field(
        default=None, description="The reason for rejection, if applicable."
    )
    reviewer_name: str = Field(
        description="Full name of the reviewer receiving the notification."
    )


class RequestedOfferingDecisionContext(BaseModel):
    site_name: str = Field(description="Name of the site from settings.")
    offering_name: str = Field(description="Name of the offering.")
    call_name: str = Field(description="Name of the call.")
    provider_name: str = Field(description="Name of the offering's provider.")
    decision: str = Field(description="The state of the request (e.g., 'accepted').")
    decision_date: Any = Field(description="The date the decision was made.")
    call_url: str = Field(description="A URL to the call management page.")


class RoundOpeningForReviewersContext(BaseModel):
    site_name: str = Field(description="Name of the site from settings.")
    call_name: str = Field(description="Name of the call.")
    round_name: str = Field(description="Name of the round that is opening.")
    start_date: Any = Field(description="The start date and time of the round.")
    end_date: Any = Field(description="The end date and time of the round.")
    call_url: str = Field(description="A URL to the call page.")
    reviewer_name: str = Field(
        description="Full name of the reviewer receiving the notification."
    )


class RoundClosingForManagersContext(BaseModel):
    site_name: str = Field(description="Name of the site from settings.")
    call_name: str = Field(description="Name of the call.")
    round_name: str = Field(description="Name of the round that has closed.")
    total_proposals: int = Field(
        description="The total number of proposals submitted in this round."
    )
    start_date: Any = Field(description="The round start date.")
    close_date: Any = Field(description="The round close date.")
    total_reviews: int = Field(
        description="The number of reviews recorded against this round's proposals, "
        "excluding rejected ones."
    )
    round_url: str = Field(description="A URL to the round management page.")


class ProposalSubmissionDeadlineApproachingContext(BaseModel):
    site_name: str = Field(description="Name of the site from settings.")
    proposal_creator_name: str = Field(
        description="Full name of the proposal creator receiving the reminder."
    )
    proposal_name: str = Field(description="Name of the draft proposal.")
    call_name: str = Field(description="Name of the call for proposals.")
    round_name: str = Field(description="Name of the round containing the proposal.")
    deadline_date: Any = Field(
        description="Proposal submission deadline date and time."
    )
    time_remaining_days: int = Field(
        description="Whole number of days remaining until the submission deadline."
    )
    time_remaining_hours: int = Field(
        description="Whole number of hours remaining after remaining days are excluded."
    )
    proposal_url: str = Field(description="A URL to the proposal details page.")


class ReviewsCompleteContext(BaseModel):
    site_name: str = Field(description="The name of the site from settings.")
    proposal_name: str = Field(
        description="The name of the proposal whose reviews are complete."
    )
    submitter_name: str = Field(
        description="The full name of the user who submitted the proposal."
    )
    call_name: str = Field(description="The name of the call for proposals.")
    reviews_count: int = Field(description="The integer count of completed reviews.")
    average_score: float = Field(
        description="The average summary score calculated from all completed reviews (a float or decimal number)."
    )
    reviews: list[dict[str, Any]] = Field(
        description="A list of dictionaries representing completed reviews, each containing `reviewer_name`, `score`, and `submitted_at`."
    )
    proposal_url: str = Field(
        description="A direct URL for the call manager to view the proposal and its completed reviews."
    )


class ReviewerInvitationContext(BaseModel):
    site_name: str = Field(description="Name of the site from settings.")
    call_name: str = Field(description="Name of the call the reviewer is invited to.")
    invited_by_name: str = Field(
        description="Full name of the user who sent the invitation."
    )
    invitation_link: str = Field(
        description="URL for the invitee to accept or decline the invitation."
    )


class AccessRequestStateChangedContext(BaseModel):
    """The applicant's state-change mail where the deployment has no calls.

    Deliberately not ProposalStateChangedContext minus a field: this message
    names no call and no round, so it does not carry ``call_name`` or
    ``review_period`` at all and a template cannot reach for them.
    """

    site_name: str = Field(description="Name of the site from settings.")
    new_state: str = Field(
        description="The new state of the access request (e.g., 'accepted')."
    )
    previous_state: str = Field(description="The previous state.")
    update_date: Any = Field(description="The timestamp of the state change.")
    proposal_url: str = Field(description="URL to the access request page.")
    project_url: str | None = Field(
        default=None,
        description="URL to the created project if the request was approved.",
    )
    project_name: str | None = Field(
        default=None, description="Name of the created project."
    )
    allocation_date: Any | None = Field(
        default=None, description="The day the granted project starts running."
    )
    duration: int | None = Field(
        default=None,
        description=(
            "How long the granted project runs, in days. None where the grant "
            "does not expire, in which case the line is not rendered."
        ),
    )
    proposal_name: str = Field(description="Name of the access request.")
    proposal_creator_name: str = Field(
        description="Full name of the person who made the request."
    )
    rejection_feedback: str | None = Field(
        default=None,
        description="Comments from the manager if the request was declined.",
    )
    allocated_resources: Any | None = Field(
        default=None, description="Resources provisioned for the new project."
    )


class ProposalSection(NotificationSection):
    class Meta:
        key = "proposal"

    new_proposal_submitted = Notification(
        key="new_proposal_submitted",
        description="Notifies call managers about a new proposal submission.",
        context_model=NewProposalSubmittedContext,
    )
    new_review_submitted = Notification(
        key="new_review_submitted",
        description="A notification to the call manager about a new review submission.",
        context_model=NewReviewSubmittedContext,
    )
    proposal_cancelled = Notification(
        key="proposal_cancelled",
        description="A notification to proposal creator about the proposal cancellation.",
        context_model=ProposalCancelledContext,
    )
    proposal_decision_for_reviewer = Notification(
        key="proposal_decision_for_reviewer",
        description="A notification to the reviewer about the proposal decision (approved/rejected) which they reviewed.",
        context_model=ProposalDecisionForReviewerContext,
    )
    proposal_state_changed = Notification(
        key="proposal_state_changed",
        description="A notification about the proposal state changes (submitted → in review → accepted/rejected).",
        context_model=ProposalStateChangedContext,
    )
    access_request_state_changed = Notification(
        key="access_request_state_changed",
        description=(
            "The same state changes, for a deployment that hides calls from "
            "applicants (SERVICE_ACCESS_MODE = 'marketplace'). Its own "
            "notification rather than a branch inside the proposal one, so "
            "each can be worded, overridden and switched off independently."
        ),
        context_model=AccessRequestStateChangedContext,
    )
    proposal_submission_deadline_approaching = Notification(
        key="proposal_submission_deadline_approaching",
        description="Reminds proposal creators to submit draft proposals during the last 3 days before the round cutoff.",
        context_model=ProposalSubmissionDeadlineApproachingContext,
    )
    requested_offering_decision = Notification(
        key="requested_offering_decision",
        description="A notification to call manager about the decision on requested offering (accepted/rejected).",
        context_model=RequestedOfferingDecisionContext,
    )
    review_assigned = Notification(
        key="review_assigned",
        description="A notification to a reviewer about a new review assignment.",
        context_model=ReviewAssignedContext,
    )
    review_deadline_approaching = Notification(
        key="review_deadline_approaching",
        description="Reminds reviewers to submit in-review assignments 3 days before deadline.",
        context_model=ReviewDeadlineApproachingContext,
    )
    review_rejected = Notification(
        key="review_rejected",
        description="A notification to the call managers about a rejected review.",
        context_model=ReviewRejectedContext,
    )
    round_closing_for_managers = Notification(
        key="round_closing_for_managers",
        description="Notifies call managers that a round has ended, with a summary of proposals and reviews.",
        context_model=RoundClosingForManagersContext,
    )
    round_opening_for_reviewers = Notification(
        key="round_opening_for_reviewers",
        description="A notification to reviewers about a new call round opening.",
        context_model=RoundOpeningForReviewersContext,
    )
    reviewer_invitation = Notification(
        key="reviewer_invitation",
        description="Sent to a person invited to join the reviewer pool for a call.",
        context_model=ReviewerInvitationContext,
    )
    reviews_complete = Notification(
        key="reviews_complete",
        description="Notifies call managers when all required reviews for a proposal have been submitted, providing a summary.",
        context_model=ReviewsCompleteContext,
    )


class JustificationReviewNotificationContext(BaseModel):
    """Context for onboarding justification review notifications."""

    user_full_name: str = Field(
        description="Full name of the user who submitted the justification."
    )
    organization_name: str = Field(
        description="Name or identifier of the organization being onboarded."
    )
    created_at: str = Field(
        description="Date and time when the verification was created."
    )
    site_name: str = Field(description="Name of the site/platform.")
    link_to_homeport_dashboard: str = Field(
        description="URL link to the user's homeport dashboard."
    )


class OnboardingSection(NotificationSection):
    """Notifications related to customer onboarding and verification."""

    class Meta:
        key = "onboarding"

    justification_review_notification = Notification(
        key="justification_review_notification",
        description="Notifies users when their onboarding justification has been reviewed.",
        context_model=JustificationReviewNotificationContext,
    )


class NotificationDigestContext(BaseModel):
    user: Any = Field(description="The User model instance receiving the digest.")
    action_count: int = Field(
        description="Total number of active actions for the user."
    )
    high_urgency_count: int = Field(
        description="Number of high urgency actions for the user."
    )
    site_name: str = Field(description="Name of the site from settings.")
    actions_url: str = Field(
        description="URL to the user actions page in the dashboard."
    )


class UserActionsSection(NotificationSection):
    class Meta:
        key = "user_actions"

    notification_digest = Notification(
        key="notification_digest",
        description="A daily digest notification sent to users with pending actions.",
        context_model=NotificationDigestContext,
    )


class ManagedProjectRejectedContext(BaseModel):
    recipient_first_name: str = Field(
        description="First name of the recipient being notified."
    )
    project_name: str = Field(
        description="Name of the project whose allocation was rejected."
    )
    reviewer_full_name: str = Field(
        description="Full name of the reviewer who rejected the request."
    )
    reviewer_email: str = Field(description="Email address of the reviewer.")
    reviewer_organization: str = Field(description="Organization of the reviewer.")
    review_comment: str = Field(
        description="Optional comment provided with the rejection."
    )
    site_name: str = Field(description="Name of the site from settings.")


class OpenPortalSection(NotificationSection):
    class Meta:
        key = "openportal"

    managed_project_rejected = Notification(
        key="managed_project_rejected",
        description="Sent to Project admins and Project managers when their resource allocation request is rejected.",
        context_model=ManagedProjectRejectedContext,
    )
