import django.dispatch

invoice_created = django.dispatch.Signal(providing_args=['invoice', 'issuer_details'])
