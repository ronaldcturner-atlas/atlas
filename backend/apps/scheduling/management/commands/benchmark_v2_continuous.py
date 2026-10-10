import io
import json
from time import perf_counter

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from apps.scheduling.optimizer import (
    _pipeline_epoch_is_productive,
    _pipeline_epoch_transition,
)


class Command(BaseCommand):
    stealth_options = (
        'stop_requested', 'progress_callback', 'runtime_clock',
        'diagnostic_state',
    )
    help = (
        'Read-only continuous Atlas v2 benchmark. Repeatedly evaluates the '
        'compiled swap neighborhood and follows authoritatively confirmed '
        'improvements without writing schedule data.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--run-number', type=int)
        parser.add_argument('--run-id', type=int)
        parser.add_argument(
            '--target-evaluations', type=int, default=100_000_000,
        )
        parser.add_argument('--max-transitions', type=int, default=100)
        parser.add_argument('--max-runtime-seconds', type=float, default=None)
        parser.add_argument(
            '--configured-runtime-seconds', type=float, default=None,
            help=(
                'Original scheduler-selected runtime. Before its halfway '
                'point, exhausted search generations reset instead of '
                'stopping.'
            ),
        )
        parser.add_argument('--minimum-rate', type=float, default=600_000.0)
        parser.add_argument('--stress-contract-count', type=int, default=0)
        parser.add_argument('--validate-sample', type=int, default=1)
        parser.add_argument('--checkpoint-interval', type=int, default=20)
        parser.add_argument('--starting-score', type=float, default=None)
        parser.add_argument('--search-seed', type=int, default=0)
        parser.add_argument('--distribution-focus', action='store_true')
        parser.add_argument('--soft-restart-moves', type=int, default=3)
        parser.add_argument('--deep-restart-moves', type=int, default=8)
        parser.add_argument(
            '--soft-restart-maximum-penalty', type=float, default=5000.0,
        )
        parser.add_argument(
            '--deep-restart-maximum-penalty', type=float, default=10000.0,
        )
        parser.add_argument(
            '--initial-swap', action='append', default=[],
            help=(
                'Resume from a prior read-only chain using '
                'left_instance:left_physician:right_instance:right_physician; '
                'may be repeated in original acceptance order.'
            ),
        )
        parser.add_argument('--json', action='store_true', dest='as_json')

    def handle(self, *args, **options):
        target_evaluations = max(int(options['target_evaluations']), 1)
        if options['run_id'] is None and options['run_number'] is None:
            raise CommandError('Provide --run-id or --run-number.')
        max_transitions = max(int(options['max_transitions']), 0)
        initial_swaps = list(options['initial_swap'])
        current_swaps = list(initial_swaps)
        best_swaps = list(initial_swaps)
        global_fingerprints = set()
        epoch_fingerprints = set()
        repeated_state_visits = 0
        neighborhoods = []
        total_evaluations = 0
        total_kernel_seconds = 0.0
        resolved_run_number = options['run_number']
        runtime_clock = options.get('runtime_clock') or perf_counter
        started = runtime_clock()
        stopped_reason = 'transition_limit'
        stop_requested = options.get('stop_requested')
        progress_callback = options.get('progress_callback')
        starting_score = options.get('starting_score')
        current_score = float(starting_score) if starting_score is not None else None
        best_score = current_score
        current_proportionality_offset = 0.0
        best_proportionality_offset = 0.0
        checkpoint_interval = max(int(options['checkpoint_interval']), 1)
        score_checkpoints = []
        kernel_runtime_cache = {}
        max_runtime_seconds = options['max_runtime_seconds']
        if max_runtime_seconds is not None:
            max_runtime_seconds = max(float(max_runtime_seconds), 0.0)
        configured_runtime_seconds = options.get('configured_runtime_seconds')
        if configured_runtime_seconds is None:
            configured_runtime_seconds = max_runtime_seconds
        if configured_runtime_seconds is not None:
            configured_runtime_seconds = max(
                float(configured_runtime_seconds), 0.0,
            )
        distribution_focus = bool(options['distribution_focus'])
        soft_restart_moves = max(int(options['soft_restart_moves']), 1)
        deep_restart_moves = max(int(options['deep_restart_moves']), 1)
        accepted_transition_count = 0
        generation_accepted_transition_count = 0
        improving_transition_count = 0
        diversification_transition_count = 0
        authoritative_candidate_rejections = 0
        primary_improvements = 0
        proportionality_improvements = 0
        epoch_start_primary = 0
        epoch_start_proportionality = 0
        epoch_kind = 'initial'
        epoch_number = 1
        consecutive_exhausted_epochs = 0
        restart_count = 0
        deep_restart_count = 0
        diversification_remaining = 0
        diversification_applied = 0
        restart_seed = 0
        tabu_instance_pairs = []
        tabu_tenure = 0
        pipeline_epochs = []
        restart_details = []
        search_generation = 1
        generation_restart_count = 0
        generation_seed = (
            20260930
            + int(options.get('search_seed') or 0) % 2_000_000_000
        )
        reset_engine_context_pending = False
        generation_attempted_focus_families = set()
        generation_available_focus_families = set()
        generation_start_primary = 0
        generation_start_proportionality = 0
        consecutive_unproductive_generations = 0
        generation_details = []
        diagnostic_state = options.get('diagnostic_state')
        rolling_transitions = []
        best_authoritative_checkpoint = None
        last_authoritative_checkpoint = None
        if isinstance(diagnostic_state, dict):
            diagnostic_state.update({
                'schema_version': 1,
                'phase': 'search_initializing',
                'checkpoint_interval': checkpoint_interval,
                'starting_score': current_score,
                'current_predicted_score': current_score,
                'best_predicted_score': best_score,
                'accepted_transitions': 0,
                'total_evaluations': 0,
                'last_authoritative_checkpoint': None,
                'best_authoritative_checkpoint': None,
                'transitions_since_last_checkpoint': rolling_transitions,
                'current_operation_chain': current_swaps,
                'best_operation_chain': best_swaps,
            })

        def update_diagnostic_state(**values):
            if isinstance(diagnostic_state, dict):
                diagnostic_state.update(values)

        def transition_diagnostic(transition):
            if transition is None:
                return None
            # Transition payloads contain only bounded scalar identifiers and,
            # for atomic patches, the exact reassignment list required to
            # reproduce the move.
            return dict(transition)

        def checkpoint_component_comparison(current_breakdown):
            if not last_authoritative_checkpoint:
                return {}, {}
            previous_breakdown = (
                last_authoritative_checkpoint.get('score_breakdown') or {}
            )
            authoritative_changes = {
                key: float(current_breakdown.get(key, 0.0))
                - float(previous_breakdown.get(key, 0.0))
                for key in set(previous_breakdown) | set(current_breakdown)
            }
            predicted_changes = {}
            for row in rolling_transitions:
                component_deltas = (
                    (row.get('transition') or {}).get(
                        'predicted_component_deltas'
                    ) or {}
                )
                for key, value in component_deltas.items():
                    predicted_changes[key] = (
                        predicted_changes.get(key, 0.0) + float(value)
                    )
            return authoritative_changes, predicted_changes

        def finish_epoch(trigger):
            nonlocal consecutive_exhausted_epochs
            nonlocal consecutive_unproductive_generations
            epoch_primary = primary_improvements - epoch_start_primary
            epoch_proportional = (
                proportionality_improvements - epoch_start_proportionality
            )
            productive = _pipeline_epoch_is_productive(
                primary_improvements=epoch_primary,
                proportionality_improvements=epoch_proportional,
                distribution_focus=distribution_focus,
            )
            pipeline_epochs.append({
                'epoch': epoch_number,
                'restart_mode': epoch_kind,
                'trigger': trigger,
                'productive': productive,
                'primary_improvements': epoch_primary,
                'proportionality_improvements': epoch_proportional,
                'best_score': best_score,
            })
            consecutive_exhausted_epochs, transition = _pipeline_epoch_transition(
                consecutive_exhausted_epochs,
                productive=productive,
                epoch_kind=epoch_kind,
            )
            if (
                transition == 'stop'
                and epoch_kind == 'deep'
                and (
                    generation_available_focus_families
                    - generation_attempted_focus_families
                )
            ):
                # A search generation is not exhausted until its structural
                # portfolio has sampled every significant penalty family.
                return 'deep_restart'
            if transition == 'stop' and epoch_kind == 'deep':
                generation_primary = (
                    primary_improvements - generation_start_primary
                )
                generation_proportional = (
                    proportionality_improvements
                    - generation_start_proportionality
                )
                generation_productive = _pipeline_epoch_is_productive(
                    primary_improvements=generation_primary,
                    proportionality_improvements=generation_proportional,
                    distribution_focus=distribution_focus,
                )
                consecutive_unproductive_generations = (
                    0 if generation_productive
                    else consecutive_unproductive_generations + 1
                )
                generation_details.append({
                    'generation': search_generation,
                    'productive': generation_productive,
                    'primary_improvements': generation_primary,
                    'proportionality_improvements': generation_proportional,
                    'best_score': best_score,
                    'completed': True,
                })
                before_halfway = bool(
                    max_runtime_seconds is not None
                    and configured_runtime_seconds is not None
                    and (
                        max_runtime_seconds - (runtime_clock() - started)
                        >= configured_runtime_seconds / 2
                    )
                )
                if (
                    before_halfway
                    or consecutive_unproductive_generations < 2
                ):
                    transition = 'generation_restart'
            return transition

        def launch_generation_reset():
            nonlocal current_swaps, current_score
            nonlocal current_proportionality_offset, epoch_fingerprints
            nonlocal diversification_remaining, diversification_applied
            nonlocal deep_restart_count, restart_seed
            nonlocal tabu_instance_pairs, tabu_tenure
            nonlocal epoch_kind, epoch_number
            nonlocal epoch_start_primary, epoch_start_proportionality
            nonlocal search_generation, generation_start_primary
            nonlocal generation_start_proportionality
            nonlocal consecutive_exhausted_epochs
            nonlocal generation_restart_count, generation_seed
            nonlocal generation_accepted_transition_count
            nonlocal reset_engine_context_pending
            nonlocal generation_attempted_focus_families
            nonlocal generation_available_focus_families

            current_swaps = list(best_swaps)
            current_score = best_score
            current_proportionality_offset = best_proportionality_offset
            search_generation += 1
            generation_start_primary = primary_improvements
            generation_start_proportionality = proportionality_improvements

            # Recreate the state of a newly launched optimizer run. The only
            # retained search artifacts are the best schedule and the parent
            # run's original deadline.
            consecutive_exhausted_epochs = 0
            deep_restart_count = 0
            generation_restart_count = 0
            generation_accepted_transition_count = 0
            generation_seed = (
                20260930
                + int(resolved_run_number or options['run_id'] or 0) * 1009
                + int(options.get('search_seed') or 0) % 2_000_000_000
                + search_generation * 1_000_003
            )
            restart_seed = generation_seed
            diversification_remaining = 0
            diversification_applied = 0
            tabu_instance_pairs = []
            tabu_tenure = 0
            epoch_fingerprints = set()
            epoch_kind = 'initial'
            epoch_number += 1
            epoch_start_primary = primary_improvements
            epoch_start_proportionality = proportionality_improvements
            generation_attempted_focus_families = set()
            generation_available_focus_families = set()
            # Keep the run's database inputs, contracts, and requests frozen
            # across search-generation resets. A reset starts a new search
            # strategy from the best state; it must not adopt edits made while
            # the optimizer is already running. The compiled engine context is
            # reset separately below.
            reset_engine_context_pending = True

        def launch_restart(restart_mode):
            nonlocal current_swaps, current_score
            nonlocal current_proportionality_offset, epoch_fingerprints
            nonlocal diversification_remaining, diversification_applied
            nonlocal restart_count, deep_restart_count, restart_seed
            nonlocal tabu_instance_pairs, tabu_tenure
            nonlocal epoch_kind, epoch_number
            nonlocal epoch_start_primary, epoch_start_proportionality
            nonlocal generation_restart_count, generation_seed
            current_swaps = list(best_swaps)
            current_score = best_score
            current_proportionality_offset = best_proportionality_offset
            epoch_fingerprints = set()
            if generation_restart_count == 0:
                generation_seed = (
                    20260930
                    + int(resolved_run_number or options['run_id'] or 0) * 1009
                    + int(options.get('search_seed') or 0) % 2_000_000_000
                    + search_generation * 1_000_003
                )
            restart_count += 1
            generation_restart_count += 1
            if restart_mode == 'deep_restart':
                deep_restart_count += 1
            restart_seed = (
                generation_seed + generation_restart_count * 7919
            )
            diversification_remaining = (
                deep_restart_moves
                if restart_mode == 'deep_restart'
                else soft_restart_moves
            )
            diversification_applied = 0
            tabu_instance_pairs = []
            tabu_tenure = 0
            epoch_kind = 'deep' if restart_mode == 'deep_restart' else 'soft'
            epoch_number += 1
            epoch_start_primary = primary_improvements
            epoch_start_proportionality = proportionality_improvements
            restart_details.append({
                'restart': restart_count,
                'restart_mode': epoch_kind,
                'seed': restart_seed,
                'starting_score': best_score,
                'planned_diversification_moves': diversification_remaining,
                'search_generation': search_generation,
                'generation_restart': generation_restart_count,
            })

        for neighborhood_index in range(max_transitions + 1):
            update_diagnostic_state(
                phase='evaluating_neighborhood',
                runtime_seconds=float(runtime_clock() - started),
                neighborhood_index=neighborhood_index,
                epoch=epoch_number,
                restart_mode=epoch_kind,
                selection_mode=(
                    'diversify' if diversification_remaining > 0
                    else 'improve'
                ),
                search_generation=search_generation,
                restart_count=restart_count,
                accepted_transitions=accepted_transition_count,
                total_evaluations=total_evaluations,
                current_predicted_score=current_score,
                best_predicted_score=best_score,
                current_operation_chain=current_swaps,
                best_operation_chain=best_swaps,
            )
            if stop_requested is not None and stop_requested():
                stopped_reason = 'user_requested'
                break
            if (
                max_runtime_seconds is not None
                and runtime_clock() - started >= max_runtime_seconds
            ):
                stopped_reason = 'runtime_limit'
                break
            selection_mode = (
                'diversify' if diversification_remaining > 0 else 'improve'
            )
            output = io.StringIO()
            checkpoint_score = (
                accepted_transition_count == 0
                or selection_mode == 'diversify'
                or (
                    generation_accepted_transition_count > 0
                    and generation_accepted_transition_count
                    % checkpoint_interval == 0
                )
            )
            maximum_diversification_penalty = (
                float(options['deep_restart_maximum_penalty'])
                if epoch_kind == 'deep'
                else float(options['soft_restart_maximum_penalty'])
            )
            call_command(
                'benchmark_v2_swap_kernel',
                run_id=options['run_id'],
                run_number=options['run_number'],
                minimum_rate=options['minimum_rate'],
                validate_sample=max(int(options['validate_sample']), 0),
                checkpoint_score=checkpoint_score,
                runtime_cache=kernel_runtime_cache,
                stress_contract_count=max(
                    int(options['stress_contract_count']), 0,
                ),
                selection_mode=selection_mode,
                diversification_seed=(
                    restart_seed + diversification_applied * 104729
                ),
                structural_reconstruction=(
                    epoch_kind == 'deep' and diversification_applied == 0
                ),
                reset_engine_context=reset_engine_context_pending,
                exclude_structural_focus_family=sorted(
                    generation_attempted_focus_families
                ),
                deep_restart_number=deep_restart_count,
                minimum_diversification_penalty=(
                    1.0 if epoch_kind == 'deep' else 0.0
                ),
                maximum_diversification_penalty=(
                    maximum_diversification_penalty
                ),
                exclude_instance_pair=[
                    f'{first}:{second}'
                    for first, second in tabu_instance_pairs
                ],
                in_memory_swap=current_swaps,
                as_json=True,
                stdout=output,
            )
            reset_engine_context_pending = False
            lines = [line for line in output.getvalue().splitlines() if line.strip()]
            if not lines:
                raise CommandError('The v2 neighborhood benchmark returned no result.')
            kernel_result = json.loads(lines[-1])
            structural_attempt = kernel_result.get(
                'structural_reconstruction_attempt'
            ) or {}
            generation_available_focus_families.update(
                structural_attempt.get('available_families') or ()
            )
            selected_focus_family = structural_attempt.get('selected_family')
            if selected_focus_family:
                generation_attempted_focus_families.add(
                    selected_focus_family
                )
            resolved_run_number = int(kernel_result['run_number'])
            fingerprint = kernel_result['schedule_fingerprint']
            fingerprint_visit = (fingerprint, selection_mode)
            repeated_state = fingerprint_visit in epoch_fingerprints
            if repeated_state:
                repeated_state_visits += 1
            # A completed diversification pass intentionally evaluates the
            # resulting schedule once more in improvement mode. That is a
            # mode handoff, not a search cycle. Continue to reject revisiting
            # the same state within either mode by treating it as an exhausted
            # neighborhood, while allowing the legal cross-mode evaluation.
            else:
                epoch_fingerprints.add(fingerprint_visit)
            global_fingerprints.add(fingerprint)
            evaluated = int(kernel_result['distinct_candidate_schedules'])
            kernel_seconds = float(kernel_result['elapsed_seconds'])
            total_evaluations += evaluated
            total_kernel_seconds += kernel_seconds
            transition = kernel_result.get(
                'best_candidate_transition_validation'
            )
            rejected_transition = None
            rejection_reason = None
            if repeated_state:
                # A repeated state is a local search exhaustion signal, not a
                # reason to discard the complete schedule. Ignore any move
                # proposed from the duplicate state and advance through the
                # normal improvement/restart workflow below.
                transition = None
            elif transition is not None:
                if not transition.get('authoritative_legal'):
                    rejection_reason = 'authoritative_illegal'
                elif (
                    selection_mode == 'improve'
                    and not transition.get('authoritative_improving')
                ):
                    rejection_reason = 'authoritative_not_improving'
                elif (
                    selection_mode == 'diversify'
                    and not transition.get('accepted_for_diversification')
                ):
                    rejection_reason = 'outside_diversification_bounds'
                if rejection_reason is not None:
                    authoritative_candidate_rejections += 1
                    rejected_transition = {
                        'reason': rejection_reason,
                        'transition': transition_diagnostic(transition),
                    }
                    update_diagnostic_state(
                        phase='candidate_rejected',
                        authoritative_candidate_rejections=(
                            authoritative_candidate_rejections
                        ),
                        last_rejected_candidate=rejected_transition,
                    )
                    transition = None
            authoritative_current = kernel_result.get(
                'authoritative_current_score'
            )
            if checkpoint_score:
                if authoritative_current is None:
                    update_diagnostic_state(
                        phase='checkpoint_failure',
                        failure={
                            'kind': 'missing_authoritative_score',
                            'schedule_fingerprint': fingerprint,
                        },
                    )
                    raise CommandError(
                        'The v2 score checkpoint did not return an '
                        'authoritative current score.'
                    )
                authoritative_current = float(authoritative_current)
                authoritative_breakdown = (
                    kernel_result.get('authoritative_current_breakdown') or {}
                )
                (
                    authoritative_component_changes,
                    predicted_component_changes,
                ) = checkpoint_component_comparison(authoritative_breakdown)
                if current_score is not None and abs(
                    authoritative_current - current_score
                ) > 0.0001:
                    if accepted_transition_count == 0:
                        # The bootstrap score can become stale before V2 starts
                        # when contracts or requests change while the fresh-fill
                        # phase is completing. Establish the actual V2 baseline
                        # before accepting any compiled transition. Once search
                        # has begun, any divergence remains a hard failure.
                        best_score = authoritative_current
                        best_swaps = list(current_swaps)
                    else:
                        update_diagnostic_state(
                            phase='checkpoint_failure',
                            runtime_seconds=float(runtime_clock() - started),
                            failure={
                                'kind': 'current_score_divergence',
                                'predicted_score': current_score,
                                'authoritative_score': authoritative_current,
                                'difference': (
                                    authoritative_current - current_score
                                ),
                                'authoritative_breakdown': kernel_result.get(
                                    'authoritative_current_breakdown'
                                ),
                                'authoritative_component_changes_since_last_checkpoint': (
                                    authoritative_component_changes
                                ),
                                'predicted_component_changes_since_last_checkpoint': (
                                    predicted_component_changes
                                ),
                                'schedule_fingerprint': fingerprint,
                                'accepted_transitions': (
                                    accepted_transition_count
                                ),
                                'current_operation_chain': list(
                                    current_swaps
                                ),
                                'transitions_since_last_checkpoint': list(
                                    rolling_transitions
                                ),
                            },
                        )
                        raise CommandError(
                            'The compiled v2 score diverged from the '
                            'authoritative checkpoint.'
                        )
                current_score = authoritative_current
                if best_score is None:
                    best_score = current_score
                score_checkpoints.append({
                    'accepted_transitions': accepted_transition_count,
                    'score': current_score,
                    'epoch': epoch_number,
                    'restart_mode': epoch_kind,
                    'selection_mode': selection_mode,
                })
                checkpoint_diagnostic = {
                    'accepted_transitions': accepted_transition_count,
                    'score': current_score,
                    'score_breakdown': kernel_result.get(
                        'authoritative_current_breakdown'
                    ),
                    'schedule_fingerprint': fingerprint,
                    'epoch': epoch_number,
                    'restart_mode': epoch_kind,
                    'selection_mode': selection_mode,
                    'operation_count': len(current_swaps),
                }
                if (
                    best_authoritative_checkpoint is None
                    or current_score
                    < best_authoritative_checkpoint['score'] - 0.0001
                ):
                    best_authoritative_checkpoint = {
                        **checkpoint_diagnostic,
                        'operation_chain': list(current_swaps),
                    }
                update_diagnostic_state(
                    last_authoritative_checkpoint=checkpoint_diagnostic,
                    best_authoritative_checkpoint=(
                        best_authoritative_checkpoint
                    ),
                )
                last_authoritative_checkpoint = checkpoint_diagnostic
                rolling_transitions.clear()
            neighborhoods.append({
                'index': neighborhood_index,
                'epoch': epoch_number,
                'restart_mode': epoch_kind,
                'selection_mode': selection_mode,
                'schedule_fingerprint': fingerprint,
                'repeated_state': repeated_state,
                'evaluations': evaluated,
                'kernel_seconds': kernel_seconds,
                'preparation_seconds': float(
                    kernel_result['preparation_seconds']
                ),
                'preparation_breakdown': kernel_result.get(
                    'preparation_breakdown', {},
                ),
                'validation_seconds': float(
                    kernel_result.get('authoritative_validation', {}).get(
                        'elapsed_seconds', 0.0,
                    )
                ),
                'transition_refresh_seconds': float(
                    (kernel_result.get('in_process_transition') or {}).get(
                        'refresh_seconds', 0.0,
                    )
                ),
                'transition_refresh_breakdown': (
                    (kernel_result.get('in_process_transition') or {}).get(
                        'refresh_breakdown', {},
                    )
                ),
                'engine_context_cache_hit': bool(
                    kernel_result['engine_context_cache_hit']
                ),
                'schedules_per_second': float(
                    kernel_result['distinct_schedules_per_second']
                ),
                'meets_minimum_rate': bool(
                    kernel_result['meets_stage_one_rate']
                ),
                'transition': transition,
                'rejected_transition': rejected_transition,
            })
            if transition is None:
                if selection_mode == 'diversify' and diversification_applied:
                    diversification_remaining = 0
                    tabu_tenure = 12
                    continue
                restart_transition = finish_epoch('local_optimum')
                if restart_transition == 'stop':
                    stopped_reason = 'productivity_exhausted'
                    break
                if restart_transition == 'generation_restart':
                    launch_generation_reset()
                else:
                    launch_restart(restart_transition)
                continue
            if not transition.get('authoritative_legal'):
                raise CommandError(
                    'The vectorized v2 transition failed authoritative legality.'
                )
            if selection_mode == 'improve':
                if not transition.get('authoritative_improving'):
                    raise CommandError(
                        'The vectorized v2 transition was not authoritatively improving.'
                    )
            elif not transition.get('accepted_for_diversification'):
                raise CommandError(
                    'The v2 diversification transition exceeded its bounded '
                    'or hard-legal acceptance criteria.'
                )
            predicted_delta = float(transition['predicted_official_delta'])
            predicted_proportionality_delta = float(
                transition.get('predicted_proportionality_delta') or 0.0
            )
            if checkpoint_score and abs(
                float(transition['authoritative_official_delta'])
                - predicted_delta
            ) > 0.0001:
                update_diagnostic_state(
                    phase='checkpoint_failure',
                    runtime_seconds=float(runtime_clock() - started),
                    failure={
                        'kind': 'transition_delta_divergence',
                        'predicted_delta': predicted_delta,
                        'authoritative_delta': float(
                            transition['authoritative_official_delta']
                        ),
                        'difference': (
                            float(transition['authoritative_official_delta'])
                            - predicted_delta
                        ),
                        'transition': transition_diagnostic(transition),
                        'schedule_fingerprint': fingerprint,
                        'accepted_transitions': accepted_transition_count,
                        'current_operation_chain': list(current_swaps),
                        'transitions_since_last_checkpoint': list(
                            rolling_transitions
                        ),
                    },
                )
                raise CommandError(
                    'The compiled v2 transition delta diverged from the '
                    'authoritative checkpoint.'
                )
            if transition.get('operation') == 'patch':
                current_swaps.extend(
                    ':'.join(str(value) for value in (
                        'R', instance_id, old_physician_id, new_physician_id,
                    ))
                    for instance_id, old_physician_id, new_physician_id
                    in transition['reassignments']
                )
            elif transition.get('operation') == 'rotate':
                current_swaps.append(':'.join(str(value) for value in (
                    'C',
                    *(
                        value
                        for pair, new_physician_id in zip(
                            transition['assignment_pairs'],
                            transition['new_physician_ids'],
                        )
                        for value in (
                            pair[0], pair[1], new_physician_id,
                        )
                    ),
                )))
            elif transition.get('operation') == 'reassign':
                current_swaps.append(':'.join(str(value) for value in (
                    'R',
                    transition['instance_id'],
                    transition['old_physician_id'],
                    transition['new_physician_id'],
                )))
            else:
                current_swaps.append(':'.join(str(value) for value in (
                    transition['left_instance_id'],
                    transition['left_physician_id'],
                    transition['right_instance_id'],
                    transition['right_physician_id'],
                )))
            accepted_transition_count += 1
            generation_accepted_transition_count += 1
            rolling_transitions.append({
                'sequence': accepted_transition_count,
                'schedule_fingerprint_before': fingerprint,
                'transition': transition_diagnostic(transition),
            })
            if len(rolling_transitions) > checkpoint_interval:
                del rolling_transitions[:-checkpoint_interval]
            if current_score is not None:
                current_score += predicted_delta
            current_proportionality_offset += predicted_proportionality_delta
            if selection_mode == 'diversify':
                diversification_transition_count += 1
                diversification_applied += 1
                diversification_remaining -= 1
                if transition.get('operation') not in {
                    'patch', 'rotate', 'reassign',
                }:
                    tabu_instance_pairs.append(tuple(sorted((
                        int(transition['left_instance_id']),
                        int(transition['right_instance_id']),
                    ))))
                if diversification_remaining == 0:
                    tabu_tenure = 12
            else:
                improving_transition_count += 1
                if tabu_tenure > 0:
                    tabu_tenure -= 1
                    if tabu_tenure == 0:
                        tabu_instance_pairs = []
            if current_score is not None and (
                best_score is None or current_score < best_score - 0.0001
            ):
                best_score = current_score
                best_proportionality_offset = current_proportionality_offset
                best_swaps = list(current_swaps)
                primary_improvements += 1
            elif (
                current_score is not None
                and best_score is not None
                and abs(current_score - best_score) <= 0.0001
                and current_proportionality_offset
                < best_proportionality_offset - 1e-12
            ):
                best_proportionality_offset = current_proportionality_offset
                best_swaps = list(current_swaps)
                proportionality_improvements += 1
            update_diagnostic_state(
                phase='transition_accepted',
                runtime_seconds=float(runtime_clock() - started),
                accepted_transitions=accepted_transition_count,
                total_evaluations=total_evaluations,
                current_predicted_score=current_score,
                best_predicted_score=best_score,
                current_operation_chain=current_swaps,
                best_operation_chain=best_swaps,
            )
            if progress_callback is not None and best_score is not None:
                progress_callback(best_score)
            if total_evaluations >= target_evaluations:
                stopped_reason = 'evaluation_target'
                break

        if not pipeline_epochs or pipeline_epochs[-1]['epoch'] != epoch_number:
            epoch_primary = primary_improvements - epoch_start_primary
            epoch_proportional = (
                proportionality_improvements - epoch_start_proportionality
            )
            pipeline_epochs.append({
                'epoch': epoch_number,
                'restart_mode': epoch_kind,
                'trigger': stopped_reason,
                'productive': _pipeline_epoch_is_productive(
                    primary_improvements=epoch_primary,
                    proportionality_improvements=epoch_proportional,
                    distribution_focus=distribution_focus,
                ),
                'primary_improvements': epoch_primary,
                'proportionality_improvements': epoch_proportional,
                'best_score': best_score,
            })
        wall_seconds = runtime_clock() - started
        update_diagnostic_state(
            phase='completed',
            runtime_seconds=float(wall_seconds),
            stopped_reason=stopped_reason,
            accepted_transitions=accepted_transition_count,
            total_evaluations=total_evaluations,
            current_predicted_score=current_score,
            best_predicted_score=best_score,
            current_operation_chain=current_swaps,
            best_operation_chain=best_swaps,
        )
        aggregate_kernel_rate = (
            total_evaluations / total_kernel_seconds
            if total_kernel_seconds else 0.0
        )
        result = {
            'stage': 'V2_PRODUCTIVITY_DRIVEN_SEARCH',
            'read_only': True,
            'run_id': options['run_id'],
            'run_number': resolved_run_number,
            'stress_contract_count': max(
                int(options['stress_contract_count']), 0,
            ),
            'target_evaluations': target_evaluations,
            'total_evaluations': total_evaluations,
            'unique_evaluated_states': len(global_fingerprints),
            'unique_accepted_states': len(global_fingerprints),
            'repeated_state_visits': repeated_state_visits,
            'initial_transitions': len(initial_swaps),
            'accepted_transitions': accepted_transition_count,
            'improving_transitions': improving_transition_count,
            'diversification_transitions': diversification_transition_count,
            'authoritative_candidate_rejections': (
                authoritative_candidate_rejections
            ),
            'primary_improvements': primary_improvements,
            'proportionality_improvements': proportionality_improvements,
            'predicted_final_score': best_score,
            'score_checkpoint_interval': checkpoint_interval,
            'score_checkpoints': score_checkpoints,
            'stopped_reason': stopped_reason,
            'total_kernel_seconds': total_kernel_seconds,
            'aggregate_kernel_schedules_per_second': aggregate_kernel_rate,
            'minimum_rate': float(options['minimum_rate']),
            'max_runtime_seconds': max_runtime_seconds,
            'configured_runtime_seconds': configured_runtime_seconds,
            'all_neighborhoods_meet_minimum_rate': all(
                row['meets_minimum_rate'] for row in neighborhoods
            ),
            'wall_seconds': wall_seconds,
            'swaps': best_swaps,
            'neighborhoods': neighborhoods,
            'pipeline_epochs': pipeline_epochs,
            'restart_details': restart_details,
            'restart_count': restart_count,
            'search_generation': search_generation,
            'generation_details': generation_details,
            'generation_attempted_focus_families': sorted(
                generation_attempted_focus_families
            ),
            'generation_available_focus_families': sorted(
                generation_available_focus_families
            ),
            'consecutive_unproductive_generations': (
                consecutive_unproductive_generations
            ),
            'consecutive_exhausted_pipeline_epochs': (
                consecutive_exhausted_epochs
            ),
            'distribution_focus': distribution_focus,
        }
        if options['as_json']:
            self.stdout.write(json.dumps(result, sort_keys=True))
            return
        for key, value in result.items():
            self.stdout.write(f'{key}: {value}')
