from django.dispatch import Signal

# providing_args=['allocation', 'user', 'username']
slurm_association_created = Signal()

# providing_args=['allocation', 'user']
slurm_association_deleted = Signal()
