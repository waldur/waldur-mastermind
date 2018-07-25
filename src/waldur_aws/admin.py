from django.contrib import admin

from waldur_core.structure import admin as structure_admin

from .models import AWSService, AWSServiceProjectLink, Instance, Image, Region, Size, Volume


class ImageAdmin(structure_admin.BackendModelAdmin):
    fields = 'name', 'region', 'backend_id'
    list_display = 'name', 'region', 'backend_id'
    list_filter = 'region',


class ImageInline(admin.TabularInline):
    model = Image
    extra = 1


class RegionAdmin(structure_admin.ProtectedModelMixin, structure_admin.BackendModelAdmin):
    readonly_fields = 'name', 'backend_id'
    inlines = ImageInline,


class SizeAdmin(structure_admin.ProtectedModelMixin, structure_admin.BackendModelAdmin):
    readonly_fields = 'name', 'backend_id'
    list_display = 'name', 'backend_id', 'cores', 'ram', 'disk'


class VolumeAdmin(structure_admin.ResourceAdmin):
    pass


admin.site.register(Image, ImageAdmin)
admin.site.register(Size, SizeAdmin)
admin.site.register(Region, RegionAdmin)
admin.site.register(Instance, structure_admin.VirtualMachineAdmin)
admin.site.register(Volume, VolumeAdmin)
admin.site.register(AWSService, structure_admin.ServiceAdmin)
admin.site.register(AWSServiceProjectLink, structure_admin.ServiceProjectLinkAdmin)
