from django.dispatch import Signal

# providing_args=['instance', 'plan', 'offering']
resource_imported = Signal()

# providing_args=['project', 'old_customer', 'new_customer']
project_moved = Signal()

# providing_args=['permission', 'structure']
permissions_request_approved = Signal()

# providing_args=['instance']
resource_pulled = Signal()
