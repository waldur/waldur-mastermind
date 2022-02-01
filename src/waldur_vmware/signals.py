from django.dispatch import Signal

# providing_args=['vm']
vm_created = Signal()

# providing_args=['vm']
vm_updated = Signal()
