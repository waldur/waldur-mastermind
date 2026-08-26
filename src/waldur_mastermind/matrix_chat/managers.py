from django.contrib.contenttypes.models import ContentType
from django.db.models import Q

from waldur_core.structure.managers import (
    get_connected_customers,
    get_connected_projects,
)
from waldur_core.structure.models import Customer, Project

from . import models


def get_accessible_room_ids(user):
    """Get MatrixRoom IDs accessible to the user based on project/customer roles."""
    project_ct = ContentType.objects.get_for_model(Project)
    customer_ct = ContentType.objects.get_for_model(Customer)

    connected_projects = get_connected_projects(user)
    connected_customers = get_connected_customers(user)

    # Include projects that belong to user's connected customers
    projects_via_customer = Project.objects.filter(
        customer__in=connected_customers
    ).values_list("id", flat=True)

    return models.MatrixRoom.objects.filter(
        Q(content_type=project_ct, object_id__in=connected_projects)
        | Q(content_type=project_ct, object_id__in=projects_via_customer)
        | Q(content_type=customer_ct, object_id__in=connected_customers)
    ).values_list("id", flat=True)
