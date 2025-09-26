import collections
import logging

from django.db.models import Q

from waldur_core.core.models import User
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.models import Offering, Resource

logger = logging.getLogger(__name__)


def get_user_emails_for_query(query):
    """Get email addresses for users matching the query, including notification emails."""
    users, _, _, notification_emails = get_mapping(query)
    result = []

    for user in users.values():
        if user.email:
            result.append(user.email)

    if notification_emails:
        for email in notification_emails.keys():
            result.append(email)

    return result


def get_mapping(query):
    users = {}
    user_offerings = collections.defaultdict(set)
    user_customers = collections.defaultdict(set)
    notification_emails = {}

    all_users = query.get("all_users")
    if all_users:
        users = {
            user.id: user
            for user in User.objects.filter(is_active=True).exclude(email="")
        }
        # Include notification emails for all customers targeting
        all_customers = structure_models.Customer.objects.exclude(
            notification_emails__isnull=True
        ).exclude(notification_emails="")
        for customer in all_customers:
            notification_emails.update(get_customer_notification_emails_dict(customer))
    else:
        customers = query.get("customers", [])
        offerings = query.get("offerings", [])

        if offerings:
            resources = Resource.objects.filter(
                Q(offering__in=offerings) | Q(offering__parent__in=offerings)
            ).exclude(state=ResourceStates.TERMINATED)

            # Drop resources from non-whitelisted customers
            if customers:
                resources = resources.filter(
                    project__customer_id__in=[customer.id for customer in customers]
                )

            # Use only unique customers
            resource_customer_ids = (
                resources.values_list("project__customer_id", flat=True)
                .distinct()
                .order_by()
            )

            filtered_customers = structure_models.Customer.objects.filter(
                id__in=resource_customer_ids
            )
            for customer in filtered_customers:
                customer_offering_ids = set(
                    resources.filter(project__customer=customer)
                    .values_list("offering", flat=True)
                    .distinct()
                    .order_by()
                )
                customer_offerings = Offering.objects.filter(
                    id__in=customer_offering_ids
                )
                for user in customer.get_users():
                    users[user.id] = user
                    user_offerings[user.id] = user_offerings[user.id] | set(
                        customer_offerings
                    )
                    user_customers[user.id].add(customer)

                notification_emails.update(
                    get_customer_notification_emails_dict(customer, customer_offerings)
                )

        for customer in customers:
            for user in customer.get_users():
                users[user.id] = user
                user_customers[user.id].add(customer)
            for email, info in get_customer_notification_emails_dict(customer).items():
                if email not in notification_emails:
                    notification_emails[email] = info

    return users, user_offerings, user_customers, notification_emails


def get_recipients_for_query(query):
    users, user_offerings, user_customers, notification_emails = get_mapping(query)

    result = []
    for user_id, user in users.items():
        result.append(
            {
                "full_name": user.full_name,
                "email": user.email,
                "offerings": [
                    {"uuid": offering.uuid, "name": offering.name}
                    for offering in user_offerings[user_id]
                ],
                "customers": [
                    {"uuid": customer.uuid, "name": customer.name}
                    for customer in user_customers[user_id]
                ],
            }
        )

    for _, contact_info in notification_emails.items():
        result.append(
            {
                "full_name": f"Notification email for {contact_info['customer'].name}",
                "email": contact_info["email"],
                "offerings": [
                    {"uuid": offering.uuid, "name": offering.name}
                    for offering in contact_info["offerings"]
                ],
                "customers": [
                    {
                        "uuid": contact_info["customer"].uuid,
                        "name": contact_info["customer"].name,
                    }
                ],
            }
        )

    return sorted(result, key=lambda row: row["full_name"])


def get_customer_notification_emails(customer):
    if (
        not customer
        or not hasattr(customer, "notification_emails")
        or not customer.notification_emails
    ):
        return []

    try:
        emails = []
        notification_emails_str = str(customer.notification_emails).strip()
        for email_str in notification_emails_str.split(","):
            email = email_str.strip()
            if email:
                emails.append(email)
        return emails

    except (TypeError, ValueError) as e:
        logger.warning(
            f"Failed to parse notification emails for customer {getattr(customer, 'uuid', 'unknown')}: {e}"
        )
        return []


def get_customer_notification_emails_dict(customer, offerings=None):
    notification_emails = {}
    for email in get_customer_notification_emails(customer):
        notification_emails[email] = {
            "email": email,
            "customer": customer,
            "offerings": offerings or set(),
        }
    return notification_emails
