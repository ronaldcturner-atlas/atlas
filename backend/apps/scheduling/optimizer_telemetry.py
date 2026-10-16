"""Low-overhead, bounded production telemetry for Optimizer V2."""

from collections import defaultdict


def _metric_row():
    return {
        'attempts': 0,
        'accepted': 0,
        'official_penalty_reduction': 0.0,
        'official_penalty_increase': 0.0,
    }


class ProductionOptimizerTelemetry:
    """Accumulate one small set of counters at neighborhood boundaries.

    Candidate evaluation remains untouched.  The optimizer calls this object
    once after a complete vectorized neighborhood and at existing score
    checkpoints, so telemetry cost does not scale with candidate count.
    """

    SCHEMA_VERSION = 1

    def __init__(self):
        self.neighborhood_count = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self.timings = defaultdict(float)
        self.selection_modes = defaultdict(_metric_row)
        self.operations = defaultdict(_metric_row)
        self.structural_families = defaultdict(_metric_row)
        self.first_verified_checkpoint_seconds = None
        self.best_verified_score = None
        self.best_verified_seconds = None
        self.best_verified_accepted_transitions = None

    @staticmethod
    def _record_metric(row, *, accepted, official_delta):
        row['attempts'] += 1
        if not accepted:
            return
        row['accepted'] += 1
        if official_delta is None:
            return
        delta = float(official_delta)
        if delta < 0:
            row['official_penalty_reduction'] += -delta
        elif delta > 0:
            row['official_penalty_increase'] += delta

    def record_neighborhood(
        self, *, kernel_result, selection_mode, proposed_transition,
        accepted_transition, structural_attempt, command_seconds,
    ):
        self.neighborhood_count += 1
        cache_hit = bool(kernel_result.get('engine_context_cache_hit'))
        self.cache_hits += int(cache_hit)
        self.cache_misses += int(not cache_hit)

        preparation_seconds = float(kernel_result.get('preparation_seconds') or 0.0)
        kernel_seconds = float(kernel_result.get('elapsed_seconds') or 0.0)
        validation_seconds = float(
            (kernel_result.get('authoritative_validation') or {}).get(
                'elapsed_seconds'
            ) or 0.0
        )
        refresh_seconds = float(
            (kernel_result.get('in_process_transition') or {}).get(
                'refresh_seconds'
            ) or 0.0
        )
        self.timings['command_seconds'] += float(command_seconds)
        self.timings['preparation_seconds'] += preparation_seconds
        self.timings['kernel_seconds'] += kernel_seconds
        self.timings['authoritative_validation_seconds'] += validation_seconds
        self.timings['transition_refresh_seconds'] += refresh_seconds
        self.timings['command_overhead_seconds'] += max(
            float(command_seconds)
            - preparation_seconds
            - kernel_seconds
            - validation_seconds,
            0.0,
        )

        accepted = accepted_transition is not None
        official_delta = (
            accepted_transition.get('authoritative_official_delta')
            if accepted else None
        )
        self._record_metric(
            self.selection_modes[str(selection_mode)],
            accepted=accepted,
            official_delta=official_delta,
        )

        if proposed_transition is not None:
            operation = str(proposed_transition.get('operation') or 'swap')
            self._record_metric(
                self.operations[operation],
                accepted=accepted,
                official_delta=official_delta,
            )

        selected_family = (structural_attempt or {}).get('selected_family')
        if selected_family:
            self._record_metric(
                self.structural_families[str(selected_family)],
                accepted=bool(structural_attempt.get('accepted')),
                official_delta=structural_attempt.get('score_delta'),
            )

    def record_verified_checkpoint(
        self, *, elapsed_seconds, accepted_transitions, score,
    ):
        elapsed = float(elapsed_seconds)
        verified_score = float(score)
        if self.first_verified_checkpoint_seconds is None:
            self.first_verified_checkpoint_seconds = elapsed
        if (
            self.best_verified_score is None
            or verified_score < self.best_verified_score - 0.0001
        ):
            self.best_verified_score = verified_score
            self.best_verified_seconds = elapsed
            self.best_verified_accepted_transitions = int(accepted_transitions)

    @staticmethod
    def _rounded_mapping(values):
        return {
            key: round(float(value), 6)
            for key, value in sorted(values.items())
        }

    @staticmethod
    def _metric_mapping(values):
        return {
            key: {
                **row,
                'official_penalty_reduction': round(
                    float(row['official_penalty_reduction']), 4,
                ),
                'official_penalty_increase': round(
                    float(row['official_penalty_increase']), 4,
                ),
            }
            for key, row in sorted(values.items())
        }

    def summary(self, *, wall_seconds, total_evaluations, stopped_reason):
        wall = float(wall_seconds)
        return {
            'schema_version': self.SCHEMA_VERSION,
            'collection_boundary': 'one update per vectorized neighborhood',
            'wall_seconds': wall,
            'total_evaluations': int(total_evaluations),
            'end_to_end_evaluations_per_second': (
                float(total_evaluations) / wall if wall else 0.0
            ),
            'stopped_reason': stopped_reason,
            'neighborhood_count': self.neighborhood_count,
            'engine_context_cache': {
                'hits': self.cache_hits,
                'misses': self.cache_misses,
            },
            'phase_seconds': self._rounded_mapping(self.timings),
            'selection_modes': self._metric_mapping(self.selection_modes),
            'operations': self._metric_mapping(self.operations),
            'structural_families': self._metric_mapping(
                self.structural_families
            ),
            'first_verified_checkpoint_seconds': (
                self.first_verified_checkpoint_seconds
            ),
            'best_verified_score': self.best_verified_score,
            'best_verified_seconds': self.best_verified_seconds,
            'best_verified_accepted_transitions': (
                self.best_verified_accepted_transitions
            ),
        }
