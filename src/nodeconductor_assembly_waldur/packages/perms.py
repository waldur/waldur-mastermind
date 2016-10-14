from nodeconductor.core.permissions import StaffPermissionLogic, FilteredCollaboratorsPermissionLogic
from nodeconductor.structure import models as structure_models

PERMISSION_LOGICS = (
    ('packages.PackageTemplate', StaffPermissionLogic(any_permission=True)),
    ('packages.PackageComponent', StaffPermissionLogic(any_permission=True)),
    ('packages.OpenStackPackage', FilteredCollaboratorsPermissionLogic(
        collaborators_query=[
            'tenant__service_project_link__service__customer__roles__permission_group__user',
        ],
        collaborators_filter=[
            {'tenant__service_project_link__service__customer__roles__role_type': structure_models.CustomerRole.OWNER},
        ],
        any_permission=True,
    )),
)
