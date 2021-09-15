from django.db.models import Q

from waldur_core.structure.models import Customer, CustomerRole
from waldur_mastermind.marketplace.models import Resource


def get_customer_users(customer, roles):
    users = set()
    if CustomerRole.SUPPORT in roles:
        users |= set(customer.get_support_users())
    elif CustomerRole.OWNER in roles:
        users |= set(customer.get_owners())
    else:
        users |= set(customer.get_users())
    return users


def get_project_users(project, roles):
    users = set()
    if roles:
        for role in roles:
            users |= set(project.get_users(role))
    else:
        users |= set(project.get_users())
    return users


def get_users_for_query(query):
    users = set()

    customer_division_types = query.get('customer_division_types', [])
    customers = query.get('customers', [])
    customer_roles = query.get('customer_roles', [])
    projects = query.get('projects', [])
    project_roles = query.get('project_roles', [])
    offerings = query.get('offerings', [])

    if customer_division_types:
        customers.extend(
            list(Customer.objects.filter(division__type__in=customer_division_types))
        )

    if offerings:
        related_resources = Resource.objects.filter(
            Q(offering__in=offerings) | Q(offering__parent__in=offerings)
        ).exclude(state=Resource.States.TERMINATED)

        projects = filter(
            lambda project: project.id
            in related_resources.values_list('project_id', flat=True),
            projects,
        )

        customers = filter(
            lambda customer: customer.id
            in related_resources.values_list('project__customer_id', flat=True),
            customers,
        )

    for customer in customers:
        users |= get_customer_users(customer, customer_roles)

    for project in projects:
        users |= get_project_users(project, project_roles)

    return users
