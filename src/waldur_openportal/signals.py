from django.dispatch import Signal

# providing_args=['allocation', 'user']
openportal_association_created = Signal()

# providing_args=['allocation', 'user']
openportal_association_deleted = Signal()

# providing_args=['allocation', 'user']
openportal_remote_association_created = Signal()

# providing_args=['allocation', 'user']
openportal_remote_association_deleted = Signal()
