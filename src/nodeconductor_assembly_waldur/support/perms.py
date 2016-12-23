from nodeconductor.core.permissions import StaffPermissionLogic, FilteredCollaboratorsPermissionLogic
from nodeconductor.structure import models as structure_models


PERMISSION_LOGICS = (
    ('support.Issue', FilteredCollaboratorsPermissionLogic(
        collaborators_query=[
            'customer__roles__permission_group__user',
            'project__roles__permission_group__user',
            'project__roles__permission_group__user',
        ],
        collaborators_filter=[
            {'customer__roles__role_type': structure_models.CustomerRole.OWNER},
            {'project__roles__role_type': structure_models.ProjectRole.ADMINISTRATOR},
            {'project__roles__role_type': structure_models.ProjectRole.MANAGER},
        ],
        any_permission=True,
    )),
    ('support.Comment', StaffPermissionLogic(any_permission=True)),
)
