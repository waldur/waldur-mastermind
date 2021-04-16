from django.dispatch import Signal

slurm_association_created = Signal(providing_args=['allocation', 'user', 'username'])
slurm_association_deleted = Signal(providing_args=['allocation', 'user'])
