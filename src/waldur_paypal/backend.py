import datetime
import dateutil.parser
import decimal
import paypalrestsdk as paypal
import urlparse
import urllib
import urllib2

from django.conf import settings
from django.core.files.base import ContentFile
from django.utils import six, timezone


class PayPalError(Exception):
    pass


class PaypalPayment(object):
    def __init__(self, payment_id, approval_url, token):
        self.payment_id = payment_id
        self.approval_url = approval_url
        self.token = token


class PaypalBackend(object):

    BACKEND_SERVERS_MAP = {
        'sandbox': 'https://www.sandbox.paypal.com',
        'live': 'https://www.paypal.com',
    }

    def __init__(self):
        config = settings.WALDUR_PAYPAL['BACKEND']
        self.configure(**config)

    def configure(self, mode, client_id, client_secret, currency_name, **kwargs):
        # extra method to validate required config options
        self.currency_name = currency_name

        paypal.configure({
            'mode': mode,
            'client_id': client_id,
            'client_secret': client_secret
        })

        self.server = self.BACKEND_SERVERS_MAP[mode]

    def get_payment_view_url(self, backend_invoice_id, params=None):
        invoice_url = '%s/invoice/payerView/details/%s' % (self.server, backend_invoice_id)
        if params:
            query_params = urllib.urlencode(params)
            invoice_url = '%s?%s' % (invoice_url, query_params)

        return invoice_url

    def _find_approval_url(self, links):
        for link in links:
            if link.rel == 'approval_url':
                return link.href
        raise PayPalError('Approval URL is not found')

    def _find_token(self, approval_url):
        parts = urlparse.urlparse(approval_url)
        params = urlparse.parse_qs(parts.query)
        token = params.get('token')
        if not token:
            raise PayPalError('Unable to parse token from approval_url')
        return token[0]

    def create_invoice(self, invoice):
        """
        Creates invoice with invoice items in it.
        https://developer.paypal.com/docs/api/invoicing/#definition-payment_term
        :param invoice: instance of Invoice class.
        :return: instance of Invoice with backend_id filled.
        """

        if invoice.backend_id:
            return

        if not invoice.items.count():
            raise PayPalError('"items" size must be between 1 and 100.')

        if not invoice.price:
            raise PayPalError('The total cost must not be zero.')

        phone = invoice.issuer_details.get('phone', {})
        if not phone:
            raise PayPalError('"phone" is a required attribute')

        if phone and 'country_code' not in phone:
            raise PayPalError('"phone"."country_code" is a required attribute')

        if phone and 'national_number' not in phone:
            raise PayPalError('"phone"."national_number" is a required attribute')

        invoice_details = {
            'merchant_info': {
                'email': invoice.issuer_details.get('email'),
                'business_name': invoice.issuer_details.get('company'),
                'phone': {
                    'country_code': phone.get('country_code'),
                    'national_number': phone.get('national_number'),
                },
                'address': {
                    'line1': invoice.issuer_details.get('address'),
                    'city': invoice.issuer_details.get('city'),
                    'state': invoice.issuer_details.get('state'),
                    'postal_code': invoice.issuer_details.get('postal'),
                    'country_code': invoice.issuer_details.get('country_code')
                }
            },
            'items': [
                {
                    'name': item.name,
                    'unit_of_measure': item.unit_of_measure,
                    'quantity': item.quantity,
                    'date': self._format_date(item.start.date()),
                    'unit_price': {
                        'currency': self.currency_name,
                        'value': self._format_decimal(item.unit_price),
                    }
                } for item in invoice.items.iterator()
            ],
            'tax_inclusive': False,
            'payment_term': {
                'due_date': self._format_date(invoice.end_date),
            },
            'total_amount': {
                'currency': self.currency_name,
                'value': self._format_decimal(invoice.total)
            }
            # 'logo_url': pass logo url if needed. 250x90, HTTPS. Image is not displayed o PDF atm.
        }

        if invoice.tax_percent and invoice.tax_percent > 0:
            for item in invoice_details['items']:
                item['tax'] = {
                    'name': 'VAT',
                    'percent': self._format_decimal(invoice.tax_percent),
                }

        invoice_details['billing_info'] = [
            {
                'email': invoice.customer.email,
                'business_name': invoice.customer.name,
            }
        ]

        backend_invoice = paypal.Invoice(invoice_details)

        try:
            if backend_invoice.create():
                invoice.state = backend_invoice.status
                invoice.backend_id = backend_invoice.id
                invoice.number = backend_invoice.number
                invoice.save(update_fields=['state', 'backend_id', 'number'])

                return invoice
            else:
                raise PayPalError(backend_invoice.error)
        except paypal.exceptions.ConnectionError as e:
            six.reraise(PayPalError, e)

    def send_invoice(self, invoice):
        if invoice.state != invoice.States.DRAFT:
            raise PayPalError('Invoice must be in "%s" state' % invoice.States.DRAFT)

        try:
            backend_invoice = paypal.Invoice.find(invoice.backend_id)
        except paypal.exceptions.ConnectionError as e:
            six.reraise(PayPalError, e)

        if not backend_invoice.send():
            raise PayPalError(backend_invoice.error)

        return invoice

    def pull_invoice(self, invoice):
        try:
            backend_invoice = paypal.Invoice.find(invoice.backend_id)
        except paypal.exceptions.ConnectionError as e:
            six.reraise(PayPalError, e)

        invoice.state = backend_invoice.status
        invoice.number = backend_invoice.number
        invoice.save(update_fields=['state', 'number'])
        return invoice

    def make_payment(self, amount, tax, description, return_url, cancel_url):
        """
        Make PayPal payment using Express Checkout workflow.
        https://developer.paypal.com/docs/api/payments/

        :param amount: Decimal value of payment including VAT tax.
        :param tax: Decimal value of VAT tax.
        :param description: Description of payment.
        :param return_url: Callback view URL for approved payment.
        :param cancel_url: Callback view URL for cancelled payment.
        :return: Object containing backend payment id, approval URL and token.
        """
        if amount < tax:
            raise PayPalError('Payment amount should be greater than tax.')

        payment = paypal.Payment({
            'intent': 'sale',
            'payer': {'payment_method': 'paypal'},
            'transactions': [
                {
                    'amount': {
                        'total': self._format_decimal(amount),
                        'currency': self.currency_name,
                        'details': {
                            'subtotal': self._format_decimal(amount - tax),
                            'tax': self._format_decimal(tax)
                        }
                    },
                    'description': description
                }
            ],
            'redirect_urls': {
                'return_url': return_url,
                'cancel_url': cancel_url
            }
        })

        try:
            if payment.create():
                approval_url = self._find_approval_url(payment.links)
                token = self._find_token(approval_url)
                return PaypalPayment(payment.id, approval_url, token)
            else:
                raise PayPalError(payment.error)
        except paypal.exceptions.ConnectionError as e:
            six.reraise(PayPalError, e)

    def approve_payment(self, payment_id, payer_id):
        try:
            payment = paypal.Payment.find(payment_id)
            # When payment is not found PayPal returns empty result instead of raising an exception
            if not payment:
                raise PayPalError('Payment not found')
            if payment.execute({'payer_id': payer_id}):
                return True
            else:
                raise PayPalError(payment.error)
        except paypal.exceptions.ConnectionError as e:
            six.reraise(PayPalError, e)

    def create_plan(self, amount, tax, name, description, return_url, cancel_url):
        """
        Create and activate monthly billing plan.
        https://developer.paypal.com/docs/api/payments.billing-plans

        :param amount: Decimal value of plan payment for one month including tax.
        :param tax: Decimal value of VAT tax.
        :param name: Name of the billing plan.
        :param description: Description of the billing plan.
        :param return_url: Callback view URL for approved billing plan.
        :param cancel_url: Callback view URL for cancelled billing plan.
        :return: Billing plan ID.
        """
        if amount < tax:
            raise PayPalError('Plan price should be greater than tax.')

        plan = paypal.BillingPlan({
            'name': name,
            'description': description,
            'type': 'INFINITE',
            'payment_definitions': [{
                'name': 'Monthly payment for {}'.format(name),
                'type': 'REGULAR',
                'frequency_interval': 1,
                'frequency': 'MONTH',
                'cycles': 0,
                'amount': {
                    'currency': self.currency_name,
                    'value': self._format_decimal(amount - tax)
                },
                'charge_models': [
                    {
                        'type': 'TAX',
                        'amount': {
                            'currency': self.currency_name,
                            'value': self._format_decimal(tax)
                        }
                    }
                ]
            }],
            'merchant_preferences': {
                'return_url': return_url,
                'cancel_url': cancel_url,
                'auto_bill_amount': 'YES',
            }
        })

        try:
            if plan.create() and plan.activate():
                return plan.id
            else:
                raise PayPalError(plan.error)
        except paypal.exceptions.ConnectionError as e:
            six.reraise(PayPalError, e)

    def _format_decimal(self, value):
        """
        PayPal API expects at most two decimal places with a period separator.
        """
        return "%.2f" % value

    def _format_date(self, date):
        """
        At the moment timezone is ignored as only days of resources usage are counted, not hours.
        """
        return date.strftime('%Y-%m-%d UTC')

    def create_agreement(self, plan_id, name):
        """
        Create billing agreement. On success returns approval_url and token
        """
        # PayPal does not support immediate start of agreement
        # That's why we need to increase start date by small amount of time
        start_date = datetime.datetime.utcnow() + datetime.timedelta(minutes=1)

        # PayPal does not fully support ISO 8601 format
        formatted_date = start_date.strftime('%Y-%m-%dT%H:%M:%SZ')

        agreement = paypal.BillingAgreement({
            'name': name,
            'description': 'Agreement for {}'.format(name),
            'start_date': formatted_date,
            'payer': {'payment_method': 'paypal'},
            'plan': {'id': plan_id}
        })
        try:
            if agreement.create():
                approval_url = self._find_approval_url(agreement.links)

                # PayPal does not return agreement ID until it is approved
                # That's why we need to extract token in order to identify it with agreement in DB
                token = self._find_token(approval_url)
                return approval_url, token
            else:
                raise PayPalError(agreement.error)
        except paypal.exceptions.ConnectionError as e:
            six.reraise(PayPalError, e)

    def execute_agreement(self, payment_token):
        """
        Agreement should be executed if user has approved it.
        On success returns agreement id
        """
        try:
            agreement = paypal.BillingAgreement.execute(payment_token)
            if not agreement:
                raise PayPalError('Can not execute agreement')
            return agreement.id
        except paypal.exceptions.ConnectionError as e:
            six.reraise(PayPalError, e)

    def get_agreement(self, agreement_id):
        """
        Get agreement from PayPal by ID
        """
        try:
            agreement = paypal.BillingAgreement.find(agreement_id)
            # When agreement is not found PayPal returns empty result instead of raising an exception
            if not agreement:
                raise PayPalError('Agreement not found')
            return agreement
        except paypal.exceptions.ConnectionError as e:
            six.reraise(PayPalError, e)

    def cancel_agreement(self, agreement_id):
        agreement = self.get_agreement(agreement_id)

        try:
            # Because user may cancel agreement via PayPal web UI
            # we need to distinguish it from cancel done via API
            if agreement.cancel({'note': 'Canceling the agreement by application'}):
                return True
            else:
                raise PayPalError(agreement.error)
        except paypal.exceptions.ConnectionError as e:
            six.reraise(PayPalError, e)

    def get_agreement_transactions(self, agreement_id, start_date, end_date=None):
        if not end_date:
            end_date = timezone.now()

        # If start and end date are the same PayPal raises exceptions
        # That's why we need to increase end_date by one day
        if end_date - start_date < datetime.timedelta(days=1):
            end_date += datetime.timedelta(days=1)

        formatted_start_date = start_date.strftime('%Y-%m-%d')
        formatted_end_date = end_date.strftime('%Y-%m-%d')

        agreement = self.get_agreement(agreement_id)
        try:
            data = agreement.search_transactions(formatted_start_date, formatted_end_date)
            txs = data.agreement_transaction_list
            if not txs:
                return []

            results = []
            for tx in txs:
                if tx.status != 'Completed':
                    continue
                results.append({
                    'time_stamp': dateutil.parser.parse(tx.time_stamp),
                    'transaction_id': tx.transaction_id,
                    'amount': decimal.Decimal(tx.amount.value),
                    'payer_email': tx.payer_email
                })
            return results

        except paypal.exceptions.ConnectionError as e:
            six.reraise(PayPalError, e)

    def download_invoice_pdf(self, invoice):
        if not invoice.backend_id:
            raise PayPalError('Invoice for date %s and customer %s could not be found' % (
                invoice.invoice_date.strftime('%Y-%m-%d'),
                invoice.customer.name,
            ))

        invoice_url = self.get_payment_view_url(invoice.backend_id, {'printPdfMode': 'true'})
        response = urllib2.urlopen(invoice_url)  # nosec
        content = response.read()
        invoice.pdf.save(invoice.file_name, ContentFile(content), save=True)
