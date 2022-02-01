import django.dispatch

# providing_args=['invoice', 'issuer_details']
invoice_created = django.dispatch.Signal()
