"""Version and provenance controls for reversible optimizer releases."""

import hashlib
import json
import os

from django.conf import settings


STABLE_V2_REVISION = 'v2.stable.2026-10-17'
CIRCADIAN_V2_REVISION = 'v2.circadian.1'
AVAILABILITY_V2_REVISION = 'v2.availability.1'
MULTISTART_FRESH_FILL_V2_REVISION = 'v2.multistart-freshfill.1'
MULTISTART_CIRCADIAN_V2_REVISION = (
    'v2.multistart-circadian-reconstruction.1'
)
TEMPORAL_V2_REVISION = 'v2.temporal.1'
WEEKEND_EXCHANGE_V2_REVISION = 'v2.weekend-exchange.1'
TEMPORAL_ESCALATION_V2_REVISION = 'v2.temporal-escalation.1'
TEMPORAL_TARGETING_V2_REVISION = 'v2.temporal-targeting.1'
CURRENT_SCORING_REVISION = 'score.2026-10-17.1'
SUPPORTED_V2_REVISIONS = frozenset({
    STABLE_V2_REVISION,
    CIRCADIAN_V2_REVISION,
    AVAILABILITY_V2_REVISION,
    MULTISTART_FRESH_FILL_V2_REVISION,
    MULTISTART_CIRCADIAN_V2_REVISION,
    TEMPORAL_V2_REVISION,
    WEEKEND_EXCHANGE_V2_REVISION,
    TEMPORAL_ESCALATION_V2_REVISION,
    TEMPORAL_TARGETING_V2_REVISION,
})

MULTISTART_FRESH_FILL_V2_REVISIONS = frozenset({
    MULTISTART_FRESH_FILL_V2_REVISION,
    MULTISTART_CIRCADIAN_V2_REVISION,
})
CIRCADIAN_CANDIDATE_SCORING_V2_REVISIONS = frozenset({
    CIRCADIAN_V2_REVISION,
    AVAILABILITY_V2_REVISION,
    MULTISTART_FRESH_FILL_V2_REVISION,
    MULTISTART_CIRCADIAN_V2_REVISION,
    TEMPORAL_V2_REVISION,
    WEEKEND_EXCHANGE_V2_REVISION,
    TEMPORAL_ESCALATION_V2_REVISION,
    TEMPORAL_TARGETING_V2_REVISION,
})
BLOCK_AVAILABILITY_CANDIDATE_SCORING_V2_REVISIONS = frozenset({
    AVAILABILITY_V2_REVISION,
    MULTISTART_FRESH_FILL_V2_REVISION,
    MULTISTART_CIRCADIAN_V2_REVISION,
    TEMPORAL_V2_REVISION,
    WEEKEND_EXCHANGE_V2_REVISION,
    TEMPORAL_ESCALATION_V2_REVISION,
    TEMPORAL_TARGETING_V2_REVISION,
})
TEMPORAL_RECONSTRUCTION_SCORING_V2_REVISIONS = frozenset({
    MULTISTART_CIRCADIAN_V2_REVISION,
    TEMPORAL_V2_REVISION,
    WEEKEND_EXCHANGE_V2_REVISION,
    TEMPORAL_ESCALATION_V2_REVISION,
    TEMPORAL_TARGETING_V2_REVISION,
})
BALANCED_WEEKEND_EXCHANGE_V2_REVISIONS = frozenset({
    WEEKEND_EXCHANGE_V2_REVISION,
    TEMPORAL_ESCALATION_V2_REVISION,
    TEMPORAL_TARGETING_V2_REVISION,
})
TEMPORAL_PLATEAU_ESCALATION_V2_REVISIONS = frozenset({
    TEMPORAL_ESCALATION_V2_REVISION,
    TEMPORAL_TARGETING_V2_REVISION,
})
V2_RUN_KIND = 'OPTIMIZER_V2'
LEGACY_V2_TEST_RUN_KIND = 'OPTIMIZER_V2_TEST'
V2_RUN_KINDS = frozenset({V2_RUN_KIND, LEGACY_V2_TEST_RUN_KIND})


def configured_v2_revision():
    """Return the configured revision after proving this build supports it."""
    revision = str(
        getattr(settings, 'ATLAS_OPTIMIZER_REVISION', STABLE_V2_REVISION)
    ).strip()
    return require_supported_v2_revision(revision)


def require_supported_v2_revision(revision):
    """Fail closed instead of silently running a different optimizer revision."""
    normalized = str(revision or '').strip()
    if normalized not in SUPPORTED_V2_REVISIONS:
        supported = ', '.join(sorted(SUPPORTED_V2_REVISIONS))
        raise ValueError(
            f'Optimizer revision {normalized or "(missing)"} is not supported '
            f'by this release. Supported revisions: {supported}.'
        )
    return normalized


def configured_scoring_revision():
    revision = str(
        getattr(settings, 'ATLAS_SCORING_REVISION', CURRENT_SCORING_REVISION)
    ).strip() or CURRENT_SCORING_REVISION
    if len(revision) > 64:
        raise ValueError('Scoring revision must be 64 characters or fewer.')
    return revision


def code_release_identifier():
    """Return the deployment commit/release without invoking Git at runtime."""
    return next((
        value.strip()
        for value in (
            os.environ.get('ATLAS_RELEASE'),
            os.environ.get('RAILWAY_GIT_COMMIT_SHA'),
            os.environ.get('SOURCE_VERSION'),
            os.environ.get('GIT_COMMIT'),
        )
        if value and value.strip()
    ), 'development')[:128]


def configuration_digest(snapshot):
    """Hash one canonical launch snapshot for reproducible comparisons."""
    canonical = dict(snapshot)
    # Capture time and assignment-row primary keys do not affect optimizer
    # behavior. Excluding them lets equivalent cloned fixtures compare equal.
    canonical.pop('captured_at', None)
    if isinstance(canonical.get('source_assignments'), list):
        canonical['source_assignments'] = [
            {
                key: value
                for key, value in assignment.items()
                if key != 'id'
            }
            for assignment in canonical['source_assignments']
        ]
    serialized = json.dumps(
        canonical,
        sort_keys=True,
        separators=(',', ':'),
        default=str,
    ).encode('utf-8')
    return hashlib.sha256(serialized).hexdigest()


def optimizer_provenance(optimizer_run):
    return {
        'optimizer_revision': optimizer_run.optimizer_revision,
        'scoring_revision': optimizer_run.scoring_revision,
        'optimizer_commit': optimizer_run.optimizer_commit,
        'configuration_digest': optimizer_run.configuration_digest,
    }
