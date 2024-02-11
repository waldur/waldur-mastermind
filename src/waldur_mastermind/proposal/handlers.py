from waldur_mastermind.proposal import models


def create_reviews(sender, instance, created=False, **kwargs):
    proposal: models.Proposal = instance

    if created:
        return

    if not proposal.tracker.has_changed("state"):
        return

    if (
        proposal.tracker.previous("state") != models.Proposal.States.DRAFT
        or proposal.state != models.Proposal.States.SUBMITTED
    ):
        return

    if proposal.round.review_strategy != models.Round.ReviewStrategies.AFTER_PROPOSAL:
        return

    for reviewer in proposal.round.call.reviewers:
        models.Review.objects.create(reviewer=reviewer, proposal=proposal)
