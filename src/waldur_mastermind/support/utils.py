from waldur_core.core.utils import format_homeport_link


def get_feedback_link(token, evaluation=''):
    return format_homeport_link(
        'support/feedback/?token={token}&evaluation={evaluation}',
        token=token,
        evaluation=evaluation,
    )
