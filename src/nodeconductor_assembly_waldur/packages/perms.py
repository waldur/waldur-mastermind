from nodeconductor.core.permissions import StaffPermissionLogic, FilteredCollaboratorsPermissionLogic
from nodeconductor.structure import models as structure_models

PERMISSION_LOGICS = (
    ('packages.PackageTemplate', StaffPermissionLogic(any_permission=True)),
    ('packages.PackageComponent', StaffPermissionLogic(any_permission=True)),
    ('packages.OpenStackPackage', FilteredCollaboratorsPermissionLogic(
        collaborators_query=[
            'tenant__service_project_link__service__customer__permissions__user',
            'tenant__service_project_link__project__permissions__user',
        ],
        collaborators_filter=[
            {'tenant__service_project_link__service__customer__permissions__role': structure_models.CustomerRole.OWNER,
             'tenant__service_project_link__service__customer__permissions__is_active': True},
            {'tenant__service_project_link__project__permissions__role': structure_models.ProjectRole.MANAGER,
             'tenant__service_project_link__project__permissions__is_active': True},
        ],
        any_permission=True,
    )),
)
