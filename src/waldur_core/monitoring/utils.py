import datetime


def get_period(request):
    period = request.query_params.get('period')
    return period or format_period(datetime.date.today())


def format_period(date):
    return '%d-%02d' % (date.year, date.month)


def to_list(xs):
    return xs if isinstance(xs, list) else [xs]
