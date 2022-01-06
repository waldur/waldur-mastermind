import logging

from django.conf import settings

from waldur_core.structure.models import Customer, Project
from waldur_keycloak.client import KeycloakClient
from waldur_keycloak.models import CustomerGroup, ProjectGroup

logger = logging.getLogger(__name__)


class KeycloakBackend:
    def __init__(self):
        options = settings.WALDUR_KEYCLOAK
        self.client = KeycloakClient(
            base_url=options['BASE_URL'],
            realm=options['REALM'],
            client_id=options['CLIENT_ID'],
            client_secret=options['CLIENT_SECRET'],
            username=options['USERNAME'],
            password=options['PASSWORD'],
        )

    def create_project_group(self, group_id, project):
        logger.info(
            'Creating project group %s as a child of %s', project.name, group_id
        )
        project_group_id = self.client.create_child_group(group_id, project.name)
        ProjectGroup.objects.create(project=project, backend_id=project_group_id)

        # Add users to project group
        for user in project.get_users().filter(username__startswith='keycloak_f'):
            user_id = user.username.split('keycloak_f')[1]
            logger.info('Adding user %s to project group %s', user_id, project_group_id)
            self.client.add_user_to_group(user_id, project_group_id)

    def create_customer_group(self, customer):
        logger.info('Creating customer group %s', customer.name)
        group_id = self.client.create_group(customer.name)
        CustomerGroup.objects.create(customer=customer, backend_id=group_id)

        # Create project groups
        for project in customer.projects.all():
            self.create_project_group(group_id, project)

    def synchronize_groups(self):
        remote_groups = self.client.get_groups()
        local_groups = CustomerGroup.objects.all()

        remote_group_names = {group['id']: group['name'] for group in remote_groups}
        remote_subgroups_map = {
            group['id']: group['subGroups'] for group in remote_groups
        }
        remote_group_ids = set(remote_group_names.keys())

        local_group_names = {
            str(group.backend_id): group.customer.name for group in local_groups
        }
        local_group_map = {
            str(group.backend_id): group.customer for group in local_groups
        }
        local_group_ids = set(local_group_names.keys())

        # Delete remote leftover customer groups
        for group_id in remote_group_ids - local_group_ids:
            logger.info('Deleting leftover customer group %s', group_id)
            self.client.delete_group(group_id)

        # Delete local leftover customer groups
        leftovers = local_group_ids - remote_group_ids
        CustomerGroup.objects.filter(backend_id__in=leftovers).delete()

        # Update existing customer groups
        for group_id in local_group_ids & remote_group_ids:
            # Update name if it is not the same
            if remote_group_names[group_id] != local_group_names[group_id]:
                logger.info('Updating remote customer group name %s', group_id)
                self.client.update_group(group_id, local_group_names[group_id])

            remote_subgroups = remote_subgroups_map[group_id]
            remote_subgroup_map = {
                subgroup['id']: subgroup['name'] for subgroup in remote_subgroups
            }
            remote_subgroup_ids = set(remote_subgroup_map.keys())

            local_subgroups = ProjectGroup.objects.filter(
                project__customer=local_group_map[group_id]
            )
            local_subgroup_names = {
                str(subgroup.backend_id): subgroup.project.name
                for subgroup in local_subgroups
            }
            local_subgroup_map = {
                str(subgroup.backend_id): subgroup.project
                for subgroup in local_subgroups
            }

            local_subgroup_ids = set(local_subgroup_names.keys())

            # Delete remote leftover project subgroups
            for subgroup_id in remote_subgroup_ids - local_subgroup_ids:
                logger.info('Deleting leftover project group %s', subgroup_id)
                self.client.delete_group(subgroup_id)

            # Delete local leftover project groups
            leftovers = local_subgroup_ids - remote_subgroup_ids
            ProjectGroup.objects.filter(backend_id__in=leftovers).delete()

            for subgroup_id in local_subgroup_ids & remote_subgroup_ids:
                # Update name if it is not the same
                if (
                    remote_subgroup_map[subgroup_id]
                    != local_subgroup_names[subgroup_id]
                ):
                    logger.info('Updating project group %s', group_id)
                    self.client.update_group(
                        subgroup_id, local_subgroup_names[subgroup_id]
                    )

                remote_users = self.client.get_group_users(subgroup_id)
                project = local_subgroup_map[subgroup_id]
                local_users = project.get_users().filter(
                    username__startswith='keycloak_f'
                )
                local_user_ids = {
                    user.username.split('keycloak_f')[1]
                    for user in local_users.values_list('username')
                }
                remote_user_ids = {user['id'] for user in remote_users}
                for user_id in remote_user_ids - local_user_ids:
                    self.client.delete_user_from_group(user_id, subgroup_id)

            for project in Project.available_objects.filter(
                customer=local_group_map[group_id]
            ).exclude(id__in=local_subgroups.values_list('project_id', flat=True)):
                self.create_project_group(group_id, project)

        # Create missing customer groups
        for customer in Customer.objects.filter(blocked=False).exclude(
            id__in=local_groups.values_list('customer_id', flat=True)
        ):
            self.create_customer_group(customer)
