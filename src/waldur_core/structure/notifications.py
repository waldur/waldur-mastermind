from dataclasses import dataclass, field


@dataclass
class NotificationTemplate:
    path: str
    name: str


@dataclass
class Notification:
    key: str
    description: str
    context: dict[str, str] = field(default_factory=dict)
    templates: list | None = None

    def __post_init__(self):
        """
        Most of the notifications have given templates :key + _subject.txt/_message.txt/_message.html
        This method allows you to not specify those explicitly
        however you must ensure that these files exist or provide :templates argument to override this action.
        """
        if not self.templates:
            self.templates = [
                NotificationTemplate(self.key + "_subject.txt", self.key),
                NotificationTemplate(self.key + "_message.txt", self.key),
                NotificationTemplate(self.key + "_message.html", self.key),
            ]


NOTIFICATIONS = dict()


class NotificationSectionMetaclass(type):
    def __new__(self, name, bases, attrs):
        if "Meta" in attrs:
            section = {
                attrs["Meta"].key: [],
            }
            NOTIFICATIONS.update(section)
            for _, notification in attrs.items():
                if isinstance(notification, Notification):
                    section[attrs["Meta"].key].append(
                        {
                            "path": notification.key,
                            "description": notification.description,
                            "templates": notification.templates,
                            "context": notification.context,
                        }
                    )
        return type.__new__(self, name, bases, attrs)


class NotificationSection(metaclass=NotificationSectionMetaclass):
    pass


class StructureSection(NotificationSection):
    class Meta:
        key = "structure"

    notifications_profile_changes_operator = Notification(
        "notifications_profile_changes_operator",
        "A notification sent to Waldur operators when a user's profile is updated.",
        context={
            "user": "The User model instance whose profile has been updated. Provides access to fields like `user.full_name` and `user.email`.",
            "fields": "A list of dictionaries, where each dictionary represents a changed field and contains string keys: `name`, `old_value`, and `new_value`.",
            "organizations": "A queryset of Customer model instances where the user is an owner. Can be iterated over in the template.",
        },
    )

    change_email_request = Notification(
        "change_email_request",
        "A notification sent when an email change is requested. Recipient is the old email address.",
        context={
            "request": "The ChangeEmailRequest model instance. Provides access to `request.user`.",
            "link": "A string URL for the user to click to confirm the email change.",
        },
    )

    structure_role_granted = Notification(
        "structure_role_granted",
        "A notification sent when a role is granted. The recipient is the user who received the role.",
        context={
            "permission": "The Permission model instance (e.g., ProjectPermission). Provides `permission.role` and `permission.user`.",
            "structure": "The model instance where the role was granted (e.g., a Project or Customer object). Provides `structure.name`.",
        },
    )


class UserSection(NotificationSection):
    class Meta:
        key = "users"

    # Common context for all invitation notifications
    invitation_context = {
        "invitation.name": "The name of the structure (Project or Organization) the user is invited to.",
        "invitation.type": "The type of the structure, either 'project' or 'customer'.",
        "invitation.role": "The name of the role being granted.",
        "extra_invitation_text": "Any additional text provided by the inviter.",
        "sender": "The User model instance who created the invitation. Provides access to `sender.full_name` and `sender.email`.",
    }

    invitation_created = Notification(
        "invitation_created",
        "Sent to an invited user so they can accept the invitation.",
        context={
            **invitation_context,
            "link": "The unique URL for the user to accept the invitation.",
            "scope_link": "A URL to the project or organization page.",
            "site_host": "The hostname of the platform (e.g., 'waldur.example.com').",
            "reminder": "A boolean flag, set to `True` if this is a reminder for a pending invitation.",
        },
    )

    invitation_requested = Notification(
        "invitation_requested",
        "Sent to staff users so they can approve or reject a pending invitation.",
        context={
            **invitation_context,
            "approve_link": "A unique URL for staff to approve the invitation.",
            "reject_link": "A unique URL for staff to reject the invitation.",
        },
    )

    invitation_rejected = Notification(
        "invitation_rejected",
        "Sent to the invitation creator to inform them that their invitation has been rejected.",
        context=invitation_context,
    )

    invitation_approved = Notification(
        "invitation_approved",
        "Sent to a new user after their invitation is approved and a new account is created for them.",
        context={
            **invitation_context,
            "username": "The generated username for the new user.",
            "password": "The generated temporary password for the new user.",
            "link": "The unique URL for the new user to access their account.",
        },
    )

    invitation_expired = Notification(
        "invitation_expired",
        "Sent to the invitation creator to inform them that an invitation has expired.",
        context=invitation_context,
    )

    permission_request_submitted = Notification(
        "permission_request_submitted",
        "Sent to staff or customer owners about a submitted permission request.",
        context={
            "permission_request": "The PermissionRequest model instance. Provides `permission_request.created_by`, `permission_request.review_comment`, etc.",
            "requests_link": "A URL to the page for reviewing permission requests.",
        },
    )


class BookingSection(NotificationSection):
    class Meta:
        key = "booking"

    notification = Notification(
        "notification",
        "Sent to users to notify them about their upcoming bookings.",
        context={
            "user": "The User object who has the upcoming booking.",
            "bookings": "A list of upcoming Booking objects for the user. Each booking provides `booking.resource.name`, `booking.start`, `booking.end`.",
        },
    )


class InvoiceSection(NotificationSection):
    class Meta:
        key = "invoices"

    upcoming_ends_notification = Notification(
        "upcoming_ends_notification",
        "Notifies organization owners about an upcoming fixed-price contract ending.",
        context={
            "organization_name": "The name of the organization whose contract is ending.",
            "end": "The end date of the contract.",
            "contract_number": "The contract number, if available.",
        },
    )
    notification = Notification(
        "notification",
        "Sent to organization owners with a new invoice. Includes the invoice as an HTML attachment.",
        context={
            "month": "The month of the invoice as an integer.",
            "year": "The year of the invoice as an integer.",
            "customer": "The name of the customer the invoice is for.",
            "link": "A URL to view the invoice in the portal.",
        },
    )


class MarketplaceSection(NotificationSection):
    class Meta:
        key = "marketplace"

    # Common context for notifications related to a single resource
    resource_context = {
        "resource": "The Resource model instance. Provides access to `resource.name`, `resource.offering`, `resource.project`, etc."
    }

    # Common context for notifications related to a single order
    order_context = {
        "order": "The Order model instance. Provides access to `order.project`, `order.created_by`, `order.offering`, etc.",
        "site_name": "The name of the site from settings.",
    }

    notify_consumer_about_pending_order = Notification(
        "notify_consumer_about_pending_order",
        "Notifies project members with approval permissions about a pending order.",
        context={
            **order_context,
            "order_link": "A URL to the order details page.",
        },
    )

    notify_provider_about_pending_order = Notification(
        "notify_provider_about_pending_order",
        "Notifies service provider owners about a pending order for their offering.",
        context={
            **order_context,
            "order_url": "A URL to the order details page for the provider.",
        },
    )

    notification_about_stale_resources = Notification(
        "notification_about_stale_resources",
        "Notifies organization owners about active resources that have not generated costs recently.",
        context={
            "resources": "A list of dictionaries. Each dictionary contains `resource` (the Resource model instance) and `resource_url` (a string URL to its details page)."
        },
    )

    marketplace_resource_termination_scheduled_staff = Notification(
        "marketplace_resource_termination_scheduled_staff",
        "Notifies project admins/managers that a resource termination was scheduled by a staff member.",
        context={
            **resource_context,
            "user": "The User model instance who scheduled the termination.",
            "resource_url": "A URL to the resource details page.",
        },
    )

    marketplace_resource_update_succeeded = Notification(
        "marketplace_resource_update_succeeded",
        "A notification of a successful resource plan update. The recipients are all the users in the project.",
        context={
            "resource_name": "The name of the updated resource.",
            "order_user": "The full name of the user who initiated the update order.",
            "resource_old_plan": "The name of the resource's previous plan.",
            "resource_plan": "The name of the resource's new plan.",
            "support_email": "The site's support email address from settings.",
            "support_phone": "The site's support phone number from settings.",
        },
    )

    marketplace_resource_update_limits_succeeded = Notification(
        "marketplace_resource_update_limits_succeeded",
        "A notification of a successful resource limit update. The recipients are all the users in the project.",
        context={
            "resource_name": "The name of the updated resource.",
            "order_user": "The full name of the user who initiated the update order.",
            "resource_old_limits": "A formatted, human-readable list of the resource's previous limits.",
            "resource_limits": "A formatted, human-readable list of the resource's new limits.",
            "resource_old_plan": "(Optional) If the plan was also changed in the same order, this contains the name of the previous plan.",
            "resource_plan": "(Optional) If the plan was also changed in the same order, this contains the name of the new plan.",
            "support_email": "The site's support email address from settings.",
            "support_phone": "The site's support phone number from settings.",
        },
    )

    marketplace_resource_termination_scheduled = Notification(
        "marketplace_resource_termination_scheduled",
        "Notifies project admins/managers that a resource termination was scheduled.",
        context={
            **resource_context,
            "user": "The User model instance who scheduled the termination.",
            "resource_url": "A URL to the resource details page.",
        },
    )

    notification_about_project_ending = Notification(
        "notification_about_project_ending",
        "Notifies project and customer users about a project that is nearing its end date.",
        context={
            "projects": "A list of Project model instances that are ending. Each project is annotated with a `.url` attribute.",
            "user": "The User model instance receiving the notification.",
            "end_date": "The date when the projects will end.",
            "count_projects": "The number of projects in the `projects` list.",
            "delta": "The number of days until the project ends.",
        },
    )

    notification_about_resource_ending = Notification(
        "notification_about_resource_ending",
        "Notifies project users about a resource that is nearing its end date.",
        context={
            **resource_context,
            "user": "The User model instance receiving the notification.",
            "resource_url": "A URL to the resource details page.",
            "delta": "The number of days until the resource's end date.",
        },
    )

    marketplace_resource_update_limits_failed = Notification(
        "marketplace_resource_update_limits_failed",
        "A notification of failed resource limits update",
    )

    marketplace_resource_update_failed = Notification(
        "marketplace_resource_update_failed", "A notification of failed resource update"
    )

    marketplace_resource_create_failed = Notification(
        "marketplace_resource_create_failed",
        "A notification of a failed resource creation",
    )
    marketplace_resource_terminate_succeeded = Notification(
        "marketplace_resource_terminate_succeeded",
        "A notification of a successful resource termination",
    )

    marketplace_resource_terminate_failed = Notification(
        "marketplace_resource_terminate_failed",
        "A notification of a failed resource termination",
    )

    notification_to_user_that_order_been_rejected = Notification(
        "notification_to_user_that_order_been_rejected",
        "Notifies the user who created an order that it has been rejected.",
        context={
            **order_context,
            "order_url": "A URL to the rejected order's details page.",
            "order_type": "The display name of the order type (e.g., 'create', 'update').",
        },
    )


class MarketplaceRemoteSection(NotificationSection):
    class Meta:
        key = "marketplace_remote"

    notification_about_pending_project_updates = Notification(
        "notification_about_pending_project_updates",
        "Notifies customer owners about pending project update requests.",
        context={
            "project_update_request": "The ProjectUpdateRequest model instance that is pending.",
            "project_url": "A URL to the project's update requests page.",
        },
    )

    notification_about_project_details_update = Notification(
        "notification_about_project_details_update",
        "Notifies users about a completed project update request, detailing the changes.",
        context={
            "new_description": "(Optional) The new project description string.",
            "old_description": "(Optional) The old project description string.",
            "new_name": "(Optional) The new project name string.",
            "old_name": "(Optional) The old project name string.",
            "new_end_date": "(Optional) The new project end date.",
            "old_end_date": "(Optional) The old project end date.",
            "reviewed_by": "The User object who reviewed the request.",
            "project_url": "A URL to the project's page.",
        },
    )


class PolicySection(NotificationSection):
    class Meta:
        key = "marketplace_policy"

    notification_project_cost_limit = Notification(
        "notification_about_project_cost_exceeded_limit",
        "Notifies customer owners when a project's estimated cost has exceeded a defined policy limit.",
        context={
            "scope_class": "The class name of the scope, e.g., 'Project'.",
            "scope_name": "The name of the scope instance (e.g., the project's name).",
            "scope_url": "A URL to the scope's page in the portal.",
            "limit": "The cost limit that was exceeded.",
        },
    )


class SupportSection(NotificationSection):
    class Meta:
        key = "support"

    notification_comment_added = Notification(
        "notification_comment_added",
        "Notification about a new comment being added to an issue. The recipient is the issue's caller.",
        context={
            "issue": "The parent Issue model instance to which the comment was added.",
            "issue_url": "A direct URL to the issue's page.",
            "site_name": "The name of the platform.",
            "comment": "The newly created Comment model instance. Provides access to `comment.author.full_name`, `comment.created`, etc.",
            "description": "The content/text of the new comment.",
            "is_system_comment": "A boolean (`True` or `False`) indicating if the comment was auto-generated by an integration (e.g., SMAX) rather than a human user.",
        },
    )
    notification_comment_updated = Notification(
        "notification_comment_updated",
        "Notification about an update to an existing issue comment. The recipient is the issue's caller.",
        context={
            "issue": "The parent Issue model instance of the updated comment.",
            "issue_url": "A direct URL to the issue's page.",
            "site_name": "The name of the platform.",
            "comment": "The Comment model instance that was updated.",
            "description": "The new, current content of the comment.",
            "old_description": "The previous content of the comment before it was edited.",
        },
    )
    notification_issue_feedback = Notification(
        "notification_issue_feedback",
        "Notification requesting feedback after an issue has been resolved. The recipient is the issue's caller.",
        context={
            "issue": "The Issue model instance for which feedback is being requested.",
            "issue_url": "A direct URL to the issue's page.",
            "site_name": "The name of the platform.",
            "feedback_link": "A generic, signed URL to provide feedback.",
            "feedback_links": "A list of dictionaries for creating a rating scale (e.g., 1-10). Each dictionary contains a `label` (e.g., '1') and a unique `link` for that rating. You can iterate over this in the template.",
        },
    )

    notification_issue_updated = Notification(
        "notification_issue_updated",
        "Notification about one or more fields of an issue being updated. The recipient is the issue's caller.",
        context={
            "issue": "The Issue model instance that was updated. Provides access to all current issue fields.",
            "issue_url": "A direct URL to the issue's page.",
            "site_name": "The name of the platform.",
            "changed": "A dictionary containing the fields that were updated, with their old values. For example, `changed.description` would hold the previous description if it was changed.",
            "description": "The new, current description of the issue.",
            "old_description": "The previous description of the issue before the update.",
        },
    )

    # Note: The 'description' and 'summary' notifications are used for generating issue content
    # from templates when creating issues via an API, not for sending email notifications about updates.
    # Their context is primarily the issue object itself.
    description = Notification(
        "description",
        "A template used for generating the issue description field during issue creation.",
        templates=[
            NotificationTemplate("description.txt", "description"),
        ],
        context={
            "issue": "The Issue model instance being created.",
        },
    )

    summary = Notification(
        "summary",
        "A template used for generating the issue summary field during issue creation.",
        templates=[
            NotificationTemplate("summary.txt", "summary"),
        ],
        context={
            "issue": "The Issue model instance being created.",
        },
    )


class ProposalSection(NotificationSection):
    class Meta:
        key = "proposal"

    proposal_state_changed = Notification(
        "proposal_state_changed",
        "Notifies a proposal creator about a change in their proposal's state.",
        context={
            "site_name": "Name of the site from settings.",
            "new_state": "The new state of the proposal (e.g., 'Accepted').",
            "previous_state": "The previous state of the proposal.",
            "proposal_url": "URL to the proposal details page.",
            "project_url": "(Optional) URL to the created project if the proposal was accepted.",
            "project_name": "(Optional) Name of the created project.",
            "proposal_name": "Name of the proposal.",
            "proposal_creator_name": "Full name of the proposal's creator.",
            "call_name": "Name of the call for proposals.",
            "rejection_feedback": "(Optional) Comments from the manager if the proposal was rejected.",
            "allocated_resources": "(Optional) A list of dictionaries for created resources if accepted. Each dictionary contains `name`, `provider_name`, `plan_name`.",
        },
    )

    new_proposal_submitted = Notification(
        "new_proposal_submitted",
        "Notifies call managers about a new proposal submission.",
        context={
            "site_name": "Name of the site from settings.",
            "proposal_url": "URL for the call manager to view the proposal.",
            "proposal_name": "Name of the submitted proposal.",
            "proposal_creator_name": "Full name of the proposal's creator.",
            "call_name": "Name of the call.",
            "round_name": "Name of the round.",
            "submission_date": "The date and time of submission.",
        },
    )

    new_review_submitted = Notification(
        "new_review_submitted",
        "Notifies call managers about a new review submission.",
        context={
            "site_name": "Name of the site from settings.",
            "review_url": "URL for the call manager to view the review.",
            "proposal_name": "Name of the proposal being reviewed.",
            "call_name": "Name of the call.",
            "reviewer_name": "Full name of the reviewer.",
            "score": "The summary score given by the reviewer.",
            "max_score": "The maximum possible score (string '5').",
            "total_reviews": "Total number of reviews assigned for this proposal.",
            "reviews_submitted": "Number of reviews already submitted.",
            "reviews_pending": "Number of reviews still pending.",
        },
    )

    review_rejected = Notification(
        "review_rejected",
        "Notifies call managers that a review was cancelled or rejected.",
        context={
            "site_name": "Name of the site from settings.",
            "proposal_name": "Name of the proposal for the rejected review.",
            "call_name": "Name of the call.",
            "reviewer_name": "Full name of the reviewer whose review was rejected.",
            "create_review_link": "A URL for the manager to assign a new review.",
            "total_reviews": "Total number of reviews assigned for this proposal.",
            "reviews_submitted": "Number of reviews already submitted.",
            "reviews_pending": "Number of reviews still pending.",
        },
    )

    proposal_cancelled = Notification(
        "proposal_cancelled",
        "Notifies the proposal creator that their proposal has been cancelled (e.g., because the round ended).",
        context={
            "site_name": "Name of the site from settings.",
            "proposal_name": "Name of the cancelled proposal.",
            "call_name": "Name of the call.",
            "cancellation_date": "The date and time of cancellation.",
            "proposal_url": "A URL to the cancelled proposal.",
            "proposal_creator_name": "Full name of the proposal creator.",
        },
    )

    review_assigned = Notification(
        "review_assigned",
        "Notifies a user that they have been assigned to review a proposal.",
        context={
            "site_name": "Name of the site from settings.",
            "reviewer_name": "Full name of the assigned reviewer.",
            "call_name": "Name of the call.",
            "proposal_name": "Name of the proposal to be reviewed.",
            "proposal_creator_name": "Full name of the proposal's creator.",
            "review_deadline": "The deadline for submitting the review.",
            "link_to_reviews_list": "A URL to the reviewer's list of assigned reviews.",
        },
    )

    proposal_decision_for_reviewer = Notification(
        "proposal_decision_for_reviewer",
        "Notifies a reviewer about the final decision for a proposal they reviewed.",
        context={
            "site_name": "Name of the site from settings.",
            "proposal_state": "The final state of the proposal (e.g., 'Accepted').",
            "proposal_url": "A URL to the proposal.",
            "proposal_name": "Name of the proposal.",
            "call_name": "Name of the call.",
            "decision_date": "The date the decision was made.",
            "rejection_reason": "(Optional) The reason for rejection, if applicable.",
            "reviewer_name": "Full name of the reviewer receiving the notification.",
        },
    )

    requested_offering_decision = Notification(
        "requested_offering_decision",
        "Notifies call managers about a decision on a requested offering.",
        context={
            "site_name": "Name of the site from settings.",
            "offering_name": "Name of the offering.",
            "call_name": "Name of the call.",
            "provider_name": "Name of the offering's provider.",
            "decision": "The state of the request (e.g., 'Accepted').",
            "decision_date": "The date the decision was made.",
            "call_url": "A URL to the call management page.",
        },
    )

    round_opening_for_reviewers = Notification(
        "round_opening_for_reviewers",
        "Notifies reviewers that a new call round has started.",
        context={
            "site_name": "Name of the site from settings.",
            "call_name": "Name of the call.",
            "round_name": "Name of the round that is opening.",
            "start_date": "The start date and time of the round.",
            "end_date": "The end date and time of the round.",
            "call_url": "A URL to the call page.",
            "reviewer_name": "Full name of the reviewer receiving the notification.",
        },
    )

    round_closing_for_managers = Notification(
        "round_closing_for_managers",
        "Notifies call managers that a round has ended, with a summary of proposals and reviews.",
        context={
            "site_name": "Name of the site from settings.",
            "call_name": "Name of the call.",
            "round_name": "Name of the round that has closed.",
            "total_proposals": "The total number of proposals submitted in this round.",
            "total_reviews": "The total number of non-rejected reviews for this round.",
            "review_strategy": "The display name of the review strategy (e.g., 'After round').",
            "round_url": "A URL to the round management page.",
        },
    )

    reviews_complete = Notification(
        "reviews_complete",
        "Notifies call managers when all required reviews for a proposal have been submitted, providing a summary.",
        context={
            "site_name": "The name of the site from settings.",
            "proposal_name": "The name of the proposal whose reviews are complete.",
            "submitter_name": "The full name of the user who submitted the proposal.",
            "call_name": "The name of the call for proposals.",
            "reviews_count": "The integer count of completed reviews.",
            "average_score": "The average summary score calculated from all completed reviews (a float or decimal number).",
            "reviews": "A list of dictionaries, where each dictionary represents one completed review. Each item in the list contains `reviewer_name` (string), `score` (number), and `submitted_at` (datetime).",
            "proposal_url": "A direct URL for the call manager to view the proposal and its completed reviews.",
        },
    )
