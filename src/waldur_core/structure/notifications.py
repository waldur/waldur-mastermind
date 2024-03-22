from dataclasses import dataclass


@dataclass
class NotificationTemplate:
    path: str
    name: str


@dataclass
class Notification:
    key: str
    description: str
    templates: list = None

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
        "A notification sent out to notify about profile changes. The recipients are Waldur operators.",
    )

    change_email_request = Notification(
        "change_email_request",
        "A notification sent out when an email change is requested. Recipient is the old email address.",
    )

    structure_role_granted = Notification(
        "structure_role_granted",
        "A notification sent out when a role is granted. The recipient is the user who received the role.",
    )


class UserSection(NotificationSection):
    class Meta:
        key = "users"

    invitation_created = Notification(
        "invitation_created",
        "A notification sent to the user so that he can accept it and receive permissions. The recipient is the user who's being invited.",
    )

    invitation_requested = Notification(
        "invitation_requested",
        "A notification sent to staff users so that they can approve or reject invitation. The recipients are active staff users.",
    )

    invitation_rejected = Notification(
        "invitation_rejected",
        "A notification sent to notify the user that his invitation has been rejected. The recipient is the user who's being invited.",
    )

    invitation_approved = Notification(
        "invitation_approved",
        "A notification sent to notify the user that his invitation has been approved. The recipient is the user who's being invited.",
    )

    invitation_expired = Notification(
        "invitation_expired",
        "A notification sent out to notify the user that his invitation has expired. The recipient is the user who's being invited.",
    )

    permission_request_submitted = Notification(
        "permission_request_submitted",
        "A notification sent out to notify about submitted permission request. The recipients are active staff users or customer owners.",
    )


class BookingSection(NotificationSection):
    class Meta:
        key = "booking"

    notification = Notification(
        "notification",
        "A notification sent out to notify about upcoming bookings. The recipients are users who have upcoming bookings.",
    )


class InvoiceSection(NotificationSection):
    class Meta:
        key = "invoices"

    upcoming_ends_notification = Notification(
        "upcoming_ends_notification",
        "A notification about upcoming contract ending. The recipients are organization owners.",
    )
    notification = Notification(
        "notification",
        "A notification of invoice. The recipients are organization owners.",
    )


class MarketplaceSection(NotificationSection):
    class Meta:
        key = "marketplace"

    notify_consumer_about_pending_order = Notification(
        "notify_consumer_about_pending_order",
        "A notification for consumer about pending order. The recipients are users that have permissions to approve the order.",
    )

    notify_provider_about_pending_order = Notification(
        "notify_provider_about_pending_order",
        "A notification for provider about pending order. The recipients are users that have permissions to approve the order.",
    )

    notification_usages = Notification(
        "notification_usages",
        "A notification about usages. The recipients are organization owners.",
    )

    notification_about_stale_resources = Notification(
        "notification_about_stale_resources",
        "A notification about stale resources. The recipients are organization owners.",
    )

    marketplace_resource_termination_scheduled_staff = Notification(
        "marketplace_resource_termination_scheduled_staff",
        "A notification of a resource termination. The recipients are project administrators and managers.",
    )

    marketplace_resource_update_succeeded = Notification(
        "marketplace_resource_update_succeeded",
        "A notification of a successful resource update. The recipients are all the users in the project.",
    )

    marketplace_resource_update_limits_succeeded = Notification(
        "marketplace_resource_update_limits_succeeded",
        "A notification of a successful resource limit update. The recipients are all the users in the project.",
    )

    marketplace_resource_create_succeeded = Notification(
        "marketplace_resource_create_succeeded",
        "A notification of a successful resource creation",
    )

    marketplace_resource_termination_scheduled = Notification(
        "marketplace_resource_termination_scheduled",
        "A notification of a scheduled resource termination. The recipients are project administrators and managers",
    )

    notification_about_project_ending = Notification(
        "notification_about_project_ending",
        "A notification about project ending. The recipients are project managers and customer owners.",
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


class RancherSection(NotificationSection):
    class Meta:
        key = "rancher"

    notification_create_user = Notification(
        "notification_create_user",
        "A notification for created rancher user. The recipients is the user who requested the creation.",
    )


class MarketplaceRemoteSection(NotificationSection):
    class Meta:
        key = "marketplace_remote"

    notification_about_pending_project_updates = Notification(
        "notification_about_pending_project_updates",
        "A notification about pending project updates. The recipients are customer owners",
    )

    notification_about_project_details_update = Notification(
        "notification_about_project_details_update",
        "A notification about project details update. The recipients the user who requested project details update and the user that reviewed it.",
    )


class PolicySection(NotificationSection):
    class Meta:
        key = "marketplace_policy"

    notification_project_cost_limit = Notification(
        "notification_about_project_cost_exceeded_limit",
        "Notification about project cost exceeded limit. The recipients are all customer owners of the project.",
    )


class SupportSection(NotificationSection):
    class Meta:
        key = "support"

    notification_comment_added = Notification(
        "notification_comment_added",
        "Notification about a new comment in the issue. The recipient is issue caller.",
        templates=[
            NotificationTemplate(
                "notification_comment_added.txt", "notification_comment_added"
            ),
            NotificationTemplate(
                "notification_comment_added.html", "notification_comment_added"
            ),
            NotificationTemplate(
                "notification_comment_added_subject.txt", "notification_comment_added"
            ),
        ],
    )
    notification_comment_updated = Notification(
        "notification_comment_updated",
        "Notification about an update in the issue comment. The recipient is issue caller.",
        templates=[
            NotificationTemplate(
                "notification_comment_updated.txt", "notification_comment_added"
            ),
            NotificationTemplate(
                "notification_comment_updated.html", "notification_comment_added"
            ),
            NotificationTemplate(
                "notification_comment_updated_subject.txt", "notification_comment_added"
            ),
        ],
    )
    notification_issue_feedback = Notification(
        "notification_issue_feedback",
        "Notification about a feedback related to the issue. The recipient is issue caller.",
        templates=[
            NotificationTemplate(
                "notification_issue_feedback.txt", "notification_issue_feedback"
            ),
            NotificationTemplate(
                "notification_issue_feedback.html", "notification_issue_feedback"
            ),
            NotificationTemplate(
                "notification_issue_feedback_subject.txt", "notification_issue_feedback"
            ),
        ],
    )

    notification_issue_updated = Notification(
        "notification_issue_updated",
        "Notification about an update in the issue. The recipient is issue caller.",
        templates=[
            NotificationTemplate(
                "notification_issue_updated.txt", "notification_issue_updated"
            ),
            NotificationTemplate(
                "notification_issue_updated.html", "notification_issue_updated"
            ),
            NotificationTemplate(
                "notification_issue_updated_subject.txt", "notification_issue_updated"
            ),
        ],
    )

    description = Notification(
        "description",
        "A notification used for issue creation.",
        templates=[
            NotificationTemplate("description.txt", "description"),
        ],
    )

    summary = Notification(
        "summary",
        "A notification used for issue creation.",
        templates=[
            NotificationTemplate("summary.txt", "summary"),
        ],
    )
