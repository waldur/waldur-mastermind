from nodeconductor.core.permissions import StaffPermissionLogic


PERMISSION_LOGICS = (
    ('packages.PackageTemplate', StaffPermissionLogic(any_permission=True)),
    ('packages.OpenStackPackage', StaffPermissionLogic(any_permission=True)),
)
