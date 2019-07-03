from django.dispatch import Signal

vm_created = Signal(providing_args=['vm'])
vm_updated = Signal(providing_args=['vm'])
