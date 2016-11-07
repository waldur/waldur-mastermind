from nodeconductor.core.permissions import StaffPermissionLogic

PERMISSION_LOGICS = (
    ('invoices.Invoice', StaffPermissionLogic(any_permission=True)),
)
