from __future__ import unicode_literals

from datetime import timedelta

from waldur_core.core import WaldurExtension


class InvoicesExtension(WaldurExtension):
    class Settings:
        # wiki: https://opennode.atlassian.net/wiki/display/WD/Assembly+plugin+configuration
        WALDUR_INVOICES = {
            'ISSUER_DETAILS': {
                'company': 'OpenNode',
                'address': 'Lille 4-205',
                'country': 'Estonia',
                'email': 'info@opennodecloud.com',
                'postal': '80041',
                'phone': {
                    'country_code': '372',
                    'national_number': '5555555',
                },
                'bank': 'Estonian Bank',
                'account': '123456789',
                'vat_code': 'EE123456789',
                'country_code': 'EE',
            },
            # How many days are given to pay for created invoice
            'PAYMENT_INTERVAL': 30,
            'INVOICE_REPORTING': {
                'ENABLE': False,
                'EMAIL': 'accounting@waldur.example.com',
                'CSV_PARAMS': {
                    'delimiter': str(';'),
                },
                'USE_SAF': False,
                'SERIALIZER_EXTRA_KWARGS': {
                    'start': {
                        'format': '%d.%m.%Y',
                    },
                    'end': {
                        'format': '%d.%m.%Y',
                    }
                },
                'SAF_PARAMS': {
                    'RMAKSULIPP': '20%',
                    'ARTPROJEKT': 'PROJEKT',
                }
            },
            # Default downtime duration may vary from 1 day to 30 days.
            'DOWNTIME_DURATION_MINIMAL': timedelta(days=1),
            'DOWNTIME_DURATION_MAXIMAL': timedelta(days=30),
        }

    @staticmethod
    def django_app():
        return 'waldur_mastermind.invoices'

    @staticmethod
    def rest_urls():
        from .urls import register_in
        return register_in

    @staticmethod
    def is_assembly():
        return True

    @staticmethod
    def celery_tasks():
        from celery.schedules import crontab
        return {
            'waldur-create-invoices': {
                'task': 'invoices.create_monthly_invoices',
                'schedule': crontab(minute=0, hour=0, day_of_month='1'),
                'args': (),
            },
            'update-invoices-current-cost': {
                'task': 'invoices.update_invoices_current_cost',
                'schedule': timedelta(hours=24),
                'args': (),
            }
        }
