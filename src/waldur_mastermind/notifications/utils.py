import collections

from django.db.models import Q

from waldur_core.structure.models import Customer, Project
from waldur_mastermind.marketplace.models import Resource


def get_grouped_users_for_query(query):
    customer_users = set()
    project_users = set()

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

        project_ids = related_resources.values_list('project_id', flat=True)
        if projects:
            projects = filter(lambda project: project.id in project_ids, projects)
        # If customer role is specified we should not include all project users
        elif not customer_roles:
            projects = Project.available_objects.filter(id__in=project_ids)

        customer_ids = related_resources.values_list('project__customer_id', flat=True)
        if customers:
            customers = filter(lambda customer: customer.id in customer_ids, customers)
        else:
            customers = Customer.objects.filter(id__in=customer_ids)

    for customer in customers:
        if customer_roles or project_roles:
            for role in customer_roles:
                customer_users |= set(customer.get_users_by_role(role))
        else:
            # If both customer and project roles are not specified,
            # we should include all project users as well.
            customer_users |= set(customer.get_users())

    for project in projects:
        if project_roles:
            for role in project_roles:
                project_users |= set(project.get_users(role))
        else:
            project_users |= set(project.get_users())

    return {'project_users': project_users, 'customer_users': customer_users}


def get_users_for_query(query):
    users = get_grouped_users_for_query(query)
    return users['project_users'] | users['customer_users']


def get_recipients_for_query(query):
    users = {}
    user_offerings = collections.defaultdict(set)
    user_customers = collections.defaultdict(set)

    customers = query.get('customers', [])
    offerings = query.get('offerings', [])

    if offerings:
        resources = Resource.objects.filter(
            Q(offering__in=offerings) | Q(offering__parent__in=offerings)
        ).exclude(state=Resource.States.TERMINATED)

        for resource in resources:
            customer = resource.project.customer
            if customers and customer not in customers:
                continue
            for user in customer.get_users():
                users[user.id] = user
                user_offerings[user.id].add(resource.offering)
                user_customers[user.id].add(customer)

    for customer in customers:
        for user in customer.get_users():
            users[user.id] = user
            user_customers[user.id].add(customer)

    result = []
    for user_id, user in users.items():
        result.append(
            {
                'full_name': user.full_name,
                'email': user.email,
                'offerings': [
                    {'uuid': offering.uuid, 'name': offering.name}
                    for offering in user_offerings[user_id]
                ],
                'customers': [
                    {'uuid': customer.uuid, 'name': customer.name}
                    for customer in user_customers[user_id]
                ],
            }
        )
    return result
