from django.contrib import admin

from waldur_core.structure import admin as structure_admin

from .models import (
    Image,
    Location,
    ResourceGroup,
    SecurityGroup,
    Size,
    SQLDatabase,
    SQLServer,
    StorageAccount,
    SubNet,
    VirtualMachine,
)

admin.site.register(Location)
admin.site.register(SQLDatabase)
admin.site.register(SecurityGroup)
admin.site.register(Size)
admin.site.register(Image)
admin.site.register(SQLServer)
admin.site.register(SubNet)
admin.site.register(StorageAccount)
admin.site.register(ResourceGroup)
admin.site.register(VirtualMachine, structure_admin.VirtualMachineAdmin)
