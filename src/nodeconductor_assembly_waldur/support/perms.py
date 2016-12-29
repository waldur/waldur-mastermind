from nodeconductor.core.permissions import StaffPermissionLogic, FilteredCollaboratorsPermissionLogic
from nodeconductor.structure import models as structure_models


PERMISSION_LOGICS = (
    ('support.Issue', FilteredCollaboratorsPermissionLogic(
        collaborators_query=[
            'customer__permissions__user',
            'project__permissions__user',
            'project__permissions__user',
        ],
        collaborators_filter=[
            {'customer__permissions__role': structure_models.CustomerRole.OWNER,
             'customer__permissions__is_active': True},
            {'project__permissions__role': structure_models.ProjectRole.ADMINISTRATOR,
             'project__permissions__is_active': True},
            {'project__permissions__role': structure_models.ProjectRole.MANAGER,
             'project__permissions__is_active': True},
        ],
        any_permission=True,
    )),
    ('support.Comment', StaffPermissionLogic(any_permission=True)),
)
