from time import monotonic


class SearchBudget:
    """Separate a renewable progress deadline from a non-renewable safety cap."""

    def __init__(self, *, started_at=None, stall_seconds=60, total_seconds=600,
                 clock=monotonic, stop_requested=lambda: False):
        self.clock = clock
        self.started_at = clock() if started_at is None else started_at
        self.last_improvement = self.started_at
        self.stall_seconds = stall_seconds
        self.total_seconds = total_seconds
        self.stop_requested = stop_requested
        self.best_score = None
        self.best_coverage = None

    def observe_coverage(self, filled_slots):
        """Renew construction time only for newly filled legal slots.

        Callers supply accepted coverage, not attempted assignments. Once a
        complete valid schedule exists, only a better score renews the timer.
        """
        if self.best_score is not None or (
            self.best_coverage is not None and filled_slots <= self.best_coverage
        ):
            return False
        if self.best_coverage is not None:
            self.last_improvement = self.clock()
        self.best_coverage = filled_slots
        return True

    def observe(self, score, *, valid):
        if not valid or (self.best_score is not None and score >= self.best_score):
            return False
        # Establishing the initial score is not itself progress.
        if self.best_score is not None or self.best_coverage is not None:
            self.last_improvement = self.clock()
        self.best_score = score
        return True

    def reason(self):
        if self.stop_requested():
            return 'user_stop'
        if self.best_score == 0:
            return 'score_zero'
        now = self.clock()
        if now - self.started_at >= self.total_seconds:
            return 'overall_runtime_limit'
        if now - self.last_improvement >= self.stall_seconds:
            return 'stall_limit'
        return None
