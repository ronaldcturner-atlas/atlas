from django.db.models import Q
from django.utils import timezone

from .models import ShiftPosting, ShiftTrade


PENDING_TRADE_STATUSES = (
    ShiftTrade.Status.PENDING_RECIPIENT,
    ShiftTrade.Status.PENDING_SCHEDULER,
)


def shift_has_started(assignment, *, now=None):
    return assignment.shift_instance.start_datetime <= (now or timezone.now())


def expire_started_trade_activity(*, now=None):
    """Expire pending trades and postings as soon as an involved shift starts."""
    now = now or timezone.now()
    expired_trades = ShiftTrade.objects.filter(
        Q(offered_assignment__shift_instance__start_datetime__lte=now)
        | Q(requested_assignment__shift_instance__start_datetime__lte=now),
        status__in=PENDING_TRADE_STATUSES,
    ).update(status=ShiftTrade.Status.EXPIRED, updated_at=now)
    closed_postings = ShiftPosting.objects.filter(
        active=True,
        assignment__shift_instance__start_datetime__lte=now,
    ).update(active=False, updated_at=now)
    return expired_trades, closed_postings
