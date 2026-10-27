from types import SimpleNamespace

from django.test import SimpleTestCase

from .optimizer_lineage import (
    completed_optimizer_lineage,
    inherited_optimizer_lineage,
)


def _run(**overrides):
    values = {
        'run_number': 10,
        'start_mode': 'CURRENT_SCHEDULE',
        'optimization_focus': 'STANDARD',
        'started_from_run': None,
        'lineage_fresh_fill_run_number': None,
        'lineage_cumulative_runtime_seconds': 0.0,
        'lineage_runtime_since_penalty_reduction_seconds': 0.0,
        'lineage_search_exhaustion_count': 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class OptimizerLineageTests(SimpleTestCase):
    def test_fresh_fill_starts_its_own_lineage(self):
        result = completed_optimizer_lineage(
            _run(run_number=41, start_mode='FRESH_FILL'),
            runtime_seconds=120.0,
            initial_score=500,
            final_score=500,
            stopped_reason='runtime_limit',
            search_result={},
            search_started_elapsed_seconds=10.0,
        )

        self.assertEqual(result['lineage_fresh_fill_run_number'], 41)
        self.assertEqual(result['lineage_cumulative_runtime_seconds'], 120.0)
        self.assertEqual(
            result['lineage_runtime_since_penalty_reduction_seconds'],
            120.0,
        )
        self.assertEqual(result['lineage_search_exhaustion_count'], 0)

    def test_standard_continuation_inherits_and_adds_stalled_runtime(self):
        source = _run(
            run_number=41,
            start_mode='FRESH_FILL',
            lineage_fresh_fill_run_number=41,
            lineage_cumulative_runtime_seconds=7200.0,
            lineage_runtime_since_penalty_reduction_seconds=1800.0,
            lineage_search_exhaustion_count=1,
        )
        result = completed_optimizer_lineage(
            _run(started_from_run=source),
            runtime_seconds=3600.0,
            initial_score=500,
            final_score=500,
            stopped_reason='productivity_exhausted',
            search_result={},
            search_started_elapsed_seconds=10.0,
        )

        self.assertEqual(result['lineage_fresh_fill_run_number'], 41)
        self.assertEqual(result['lineage_cumulative_runtime_seconds'], 10800.0)
        self.assertEqual(
            result['lineage_runtime_since_penalty_reduction_seconds'],
            5400.0,
        )
        self.assertEqual(result['lineage_search_exhaustion_count'], 2)

    def test_penalty_reduction_resets_clock_to_time_after_last_reduction(self):
        source = _run(
            lineage_fresh_fill_run_number=41,
            lineage_cumulative_runtime_seconds=7200.0,
            lineage_runtime_since_penalty_reduction_seconds=1800.0,
        )
        result = completed_optimizer_lineage(
            _run(started_from_run=source),
            runtime_seconds=3600.0,
            initial_score=500,
            final_score=450,
            stopped_reason='runtime_limit',
            search_result={
                'best_authoritative_checkpoint': {
                    'elapsed_seconds': 2500.0,
                },
            },
            search_started_elapsed_seconds=300.0,
        )

        self.assertEqual(
            result['lineage_runtime_since_penalty_reduction_seconds'],
            800.0,
        )

    def test_distribution_run_pauses_penalty_stagnation_clock(self):
        source = _run(
            lineage_fresh_fill_run_number=41,
            lineage_cumulative_runtime_seconds=7200.0,
            lineage_runtime_since_penalty_reduction_seconds=1800.0,
            lineage_search_exhaustion_count=1,
        )
        result = completed_optimizer_lineage(
            _run(
                started_from_run=source,
                optimization_focus='DISTRIBUTION',
            ),
            runtime_seconds=3600.0,
            initial_score=500,
            final_score=500,
            stopped_reason='productivity_exhausted',
            search_result={},
            search_started_elapsed_seconds=20.0,
        )

        self.assertEqual(result['lineage_cumulative_runtime_seconds'], 10800.0)
        self.assertEqual(
            result['lineage_runtime_since_penalty_reduction_seconds'],
            1800.0,
        )
        self.assertEqual(result['lineage_search_exhaustion_count'], 2)

    def test_distribution_penalty_reduction_resets_clock(self):
        source = _run(
            lineage_runtime_since_penalty_reduction_seconds=1800.0,
        )
        result = completed_optimizer_lineage(
            _run(
                started_from_run=source,
                optimization_focus='DISTRIBUTION',
            ),
            runtime_seconds=1000.0,
            initial_score=500,
            final_score=490,
            stopped_reason='runtime_limit',
            search_result={
                'production_telemetry': {'best_verified_seconds': 700.0},
            },
            search_started_elapsed_seconds=100.0,
        )

        self.assertEqual(
            result['lineage_runtime_since_penalty_reduction_seconds'],
            200.0,
        )

    def test_copy_inherits_lineage_without_adding_runtime(self):
        source = _run(
            lineage_fresh_fill_run_number=41,
            lineage_cumulative_runtime_seconds=10800.0,
            lineage_runtime_since_penalty_reduction_seconds=900.0,
            lineage_search_exhaustion_count=2,
        )

        self.assertEqual(inherited_optimizer_lineage(source), {
            'lineage_fresh_fill_run_number': 41,
            'lineage_cumulative_runtime_seconds': 10800.0,
            'lineage_runtime_since_penalty_reduction_seconds': 900.0,
            'lineage_search_exhaustion_count': 2,
        })
