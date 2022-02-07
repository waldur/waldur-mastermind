def get_financial_report_url(customer):
    return f'/api/financial-reports/{customer.uuid.hex}/'
