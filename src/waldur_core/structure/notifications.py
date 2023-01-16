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
                NotificationTemplate(self.key + '_subject.txt', self.key),
                NotificationTemplate(self.key + '_message.txt', self.key),
                NotificationTemplate(self.key + '_message.html', self.key),
            ]


NOTIFICATIONS = []


class NotificationSectionMetaclass(type):
    def __new__(self, name, bases, attrs):
        if 'Meta' in attrs:
            section = {
                'key': attrs['Meta'].key,
                'items': [],
            }
            NOTIFICATIONS.append(section)
            for _, notification in attrs.items():
                if isinstance(notification, Notification):
                    section['items'].append(
                        {
                            'path': notification.key,
                            'description': notification.description,
                            'templates': notification.templates,
                        }
                    )
        return type.__new__(self, name, bases, attrs)


class NotificationSection(metaclass=NotificationSectionMetaclass):
    pass


class StructureSection(NotificationSection):
    class Meta:
        key = 'structure'

    notifications_profile_changes_operator = Notification(
        'notifications_profile_changes_operator', 'A notification of changing a profile'
    )

    change_email_request = Notification(
        'change_email_request', 'A notification of an email change request'
    )

    structure_role_granted = Notification(
        'structure_role_granted', 'A notification of a granted role'
    )


class UserSection(NotificationSection):
    class Meta:
        key = 'users'

    invitation_created = Notification(
        'invitation_created', 'A notification of invitation creation'
    )

    invitation_requested = Notification(
        'invitation_requested', 'A notification of invitation request'
    )

    invitation_rejected = Notification(
        'invitation_rejected', 'A notification of invitation rejection'
    )

    invitation_approved = Notification(
        'invitation_approved', 'A notification of invitation approval'
    )

    permission_request_submitted = Notification(
        'permission_request_submitted',
        'A notification of a submitted invitation request',
    )


class BookingSection(NotificationSection):
    class Meta:
        key = 'booking'

    notification = Notification(
        'notification', 'A notification about upcoming bookings'
    )


class InvoiceSection(NotificationSection):
    class Meta:
        key = 'invoices'

    upcoming_ends_notification = Notification(
        'upcoming_ends_notification', 'A notification about upcoming ends'
    )
    notification = Notification('notification', 'A notification of invoice')


class MarketplaceSection(NotificationSection):
    class Meta:
        key = 'marketplace'

    notification_approval = Notification(
        'notification_approval', 'A notification of order approval'
    )

    notification_service_provider_approval = Notification(
        'notification_service_provider_approval',
        'A notification to provider about pending order item approval',
    )

    notification_usages = Notification(
        'notification_usages', 'A notification about usages'
    )

    notification_about_stale_resources = Notification(
        'notification_about_stale_resources', 'A notification about stale resources'
    )

    marketplace_resource_termination_scheduled_staff = Notification(
        'marketplace_resource_termination_scheduled_staff',
        'A notification of a resource termination',
    )

    marketplace_resource_update_succeeded = Notification(
        'marketplace_resource_update_succeeded',
        'A notification of a successful resource update',
    )

    marketplace_resource_update_limits_succeeded = Notification(
        'marketplace_resource_update_limits_succeeded',
        'A notification of a successful resource limit update',
    )

    marketplace_resource_create_succeeded = Notification(
        'marketplace_resource_create_succeeded',
        'A notification of a successful resource creation',
    )

    marketplace_resource_termination_scheduled = Notification(
        'marketplace_resource_termination_scheduled',
        'A notification of a scheduled resource termination',
    )

    notification_about_project_ending = Notification(
        'notification_about_project_ending', 'A notification about project ending'
    )

    marketplace_resource_update_limits_failed = Notification(
        'marketplace_resource_update_limits_failed',
        'A notification of failed resource limits update',
    )

    marketplace_resource_update_failed = Notification(
        'marketplace_resource_update_failed', 'A notification of failed resource update'
    )

    marketplace_resource_create_failed = Notification(
        'marketplace_resource_create_failed',
        'A notification of a failed resource creation',
    )

    marketplace_resource_terminate_succeeded = Notification(
        'marketplace_resource_terminate_succeeded',
        'A notification of a successful resource termination',
    )

    marketplace_resource_terminate_failed = Notification(
        'marketplace_resource_terminate_failed',
        'A notification of a failed resource termination',
    )


class MarketplaceFlowsSection(NotificationSection):
    class Meta:
        key = 'marketplace_flows'

    flow_submitted = Notification(
        'flow_submitted', 'A notification for a submitted marketplace flow'
    )

    flow_rejected = Notification(
        'flow_rejected', 'A notification for a rejected marketplace flow'
    )


class RancherSection(NotificationSection):
    class Meta:
        key = 'rancher'

    notification_create_user = Notification(
        'notification_create_user', 'A notification for created rancher user'
    )


class MarketplaceRemoteSection(NotificationSection):
    class Meta:
        key = 'marketplace_remote'

    notification_about_pending_project_updates = Notification(
        'notification_about_pending_project_updates',
        'A notification about pending project updates',
    )
