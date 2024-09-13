from waldur_mastermind.proposal import models


def set_project_start_date(sender, instance, created=False, **kwargs):
    if created:
        return

    proposal = instance

    if (
        proposal.tracker.has_changed("state")
        and proposal.state == models.Proposal.States.ACCEPTED
    ):
        if proposal.round.allocation_time == models.Round.AllocationTimes.FIXED_DATE:
            proposal.project.start_date = proposal.round.allocation_date
            proposal.project.save()
