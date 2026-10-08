import calendar
import copy
import hashlib
import json
import secrets
from datetime import date as date_type, datetime, timedelta, timezone as datetime_timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from uuid import UUID
from time import monotonic

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import FloatField, Prefetch, Q
from django.db.models.functions import Cast
from django.db import transaction, IntegrityError
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import api_view
from rest_framework.decorators import authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.models import Physician
from apps.domains.models import Domain, DomainMembership, OrganizationMembership
from apps.domains.permissions import (
    has_permission,
    is_clinically_active,
    is_org_admin,
    permitted_domain_ids,
)
from apps.facilities.models import Facility

from .models import (
    Contract,
    ContractUserAssignment,
    OptimizerRun,
    OptimizerControl,
    ScheduleBlock,
    ScheduleCommentSeries,
    ScheduleCommentSeriesException,
    ScheduleDateComment,
    ScheduleRequest,
    ScheduleShiftAssignment,
    ScheduleShiftInstance,
    ScheduleVersion,
    Shift,
    ShiftPosting,
    ShiftStatsGroup,
    ShiftTrade,
    ShiftTradePolicy,
    ShiftTemplate,
    SharedRule,
    SharedRuleContract,
)
from .optimizer import (
    build_violation_report,
    canonical_assignment_snapshot,
    optimize_schedule_version,
    recalculate_schedule_version_score,
)
from .optimizer_v2_runner import V2_RUN_KIND
from .run_state import (
    assignments_for_viewed_run,
    get_active_optimizer_run,
    get_viewed_optimizer_run,
    resolve_build_workspace_run_context,
    serialize_run_state,
    visible_assignment_filter,
)
from .serializers import (
    ContractSerializer,
    OptimizerRunHistorySerializer,
    OptimizerRunSerializer,
    ScheduleBlockSerializer,
    ScheduleRequestSerializer,
    ScheduleShiftInstanceSerializer,
    ScheduleVersionSerializer,
    ScheduleVersionWorkspaceSerializer,
    ShiftSerializer,
    ShiftTemplateSerializer,
    SharedRuleSerializer,
)
from .shared_rules import (
    remove_shared_rule_from_contracts,
    sync_shared_rule_contract_settings,
)
from .workload_feasibility import build_workload_feasibility


STALE_OPTIMIZER_RUN_MINUTES = 10
SHIFT_TEMPLATE_DISPLAY_ORDER = (
    'facility__sort_order',
    'facility__name',
    'start_time',
    'end_time',
    'id',
)


def _ordered_shift_templates(queryset=None):
    queryset = queryset if queryset is not None else ShiftTemplate.objects.all()
    return queryset.select_related('facility', 'domain__region__organization').order_by(*SHIFT_TEMPLATE_DISPLAY_ORDER)


def _timezone_from_name(timezone_name):
    aliases = {
        'EST': 'America/New_York',
        'EDT': 'America/New_York',
        'Eastern': 'America/New_York',
        'CST': 'America/Chicago',
        'CDT': 'America/Chicago',
        'Central': 'America/Chicago',
        'MST': 'America/Denver',
        'MDT': 'America/Denver',
        'Mountain': 'America/Denver',
        'PST': 'America/Los_Angeles',
        'PDT': 'America/Los_Angeles',
        'Pacific': 'America/Los_Angeles',
    }
    try:
        return ZoneInfo(aliases.get(timezone_name, timezone_name or 'UTC'))
    except ZoneInfoNotFoundError:
        return datetime_timezone.utc


def _shift_template_fingerprint(block, templates):
    payload = {
        'block_start': block.start_date.isoformat(),
        'block_end': block.end_date.isoformat(),
        'templates': [
            {
                'id': template.id,
                'facility_id': template.facility_id,
                'facility_timezone': template.facility.timezone,
                'start_time': template.start_time.isoformat(),
                'end_time': template.end_time.isoformat(),
                'active_days_of_week': sorted(template.active_days_of_week or []),
                'default_staffing_count': template.default_staffing_count,
            }
            for template in templates
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest()


class CsrfProtectedSessionAuthentication(SessionAuthentication):
    """Session authentication with Django REST Framework's CSRF enforcement."""


@api_view(['GET', 'POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def shifts_list_create(request):
    if request.method == 'GET':
        shifts = Shift.objects.select_related(
            'facility__region__organization', 'physician', 'physician__user',
        ).all()
        if not request.user.is_superuser:
            administered_organization_ids = OrganizationMembership.objects.filter(
                user=request.user,
                active=True,
                is_org_admin=True,
            ).values_list('organization_id', flat=True)
            shifts = shifts.filter(
                Q(physician__user=request.user)
                | Q(facility__region__organization_id__in=administered_organization_ids)
            )

        facility_id = request.query_params.get('facility')
        physician_id = request.query_params.get('physician')
        month = request.query_params.get('month')
        status_filter = request.query_params.get('status')
        search = request.query_params.get('search')

        if facility_id:
            shifts = shifts.filter(facility_id=facility_id)

        if physician_id:
            shifts = shifts.filter(physician_id=physician_id)

        if month:
            try:
                year_str, month_str = month.split('-', 1)
                shifts = shifts.filter(date__year=int(year_str), date__month=int(month_str))
            except ValueError:
                return Response(
                    {'month': 'Invalid month format. Use YYYY-MM.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        if status_filter:
            shifts = shifts.filter(status=status_filter)

        if search:
            shifts = shifts.filter(
                Q(physician__user__first_name__icontains=search)
                | Q(physician__user__last_name__icontains=search)
                | Q(physician__display_name__icontains=search)
                | Q(facility__name__icontains=search)
                | Q(role__icontains=search)
                | Q(notes__icontains=search)
            )

        serializer = ShiftSerializer(shifts.distinct(), many=True)
        return Response(serializer.data)

    facility = get_object_or_404(
        Facility.objects.select_related('region__organization'),
        id=request.data.get('facility'),
    )
    if not is_org_admin(request.user, facility.region.organization):
        return Response(
            {'detail': 'Organization Administrator permission is required to create a legacy Shift.'},
            status=status.HTTP_403_FORBIDDEN,
        )
    serializer = ShiftSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    physician = serializer.validated_data['physician']
    if not OrganizationMembership.objects.filter(
        organization=facility.region.organization,
        user=physician.user,
        active=True,
    ).exists():
        return Response(
            {'detail': 'The selected user does not belong to this Facility organization.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    serializer.save()
    return Response(serializer.data, status=status.HTTP_201_CREATED)


def _accessible_published_schedule_domains(user, permission='view_published_schedules'):
    domains = Domain.objects.filter(active=True, region__active=True)
    if user.is_superuser:
        return domains
    return domains.filter(id__in=permitted_domain_ids(user, permission))


def _requested_published_schedule_domains(request, permission='view_published_schedules'):
    accessible_domains = _accessible_published_schedule_domains(request.user, permission)
    raw_domain_ids = request.query_params.get('domains') or request.query_params.get('domain')
    if not raw_domain_ids:
        return accessible_domains, None
    try:
        requested_ids = {
            int(value)
            for value in str(raw_domain_ids).split(',')
            if value.strip()
        }
    except (TypeError, ValueError):
        return None, Response(
            {'detail': 'domains must be a comma-separated list of Domain IDs.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    accessible_ids = set(accessible_domains.values_list('id', flat=True))
    if not requested_ids or not requested_ids.issubset(accessible_ids):
        return None, Response(
            {'detail': 'You do not have access to one or more selected Domains.'},
            status=status.HTTP_403_FORBIDDEN,
        )
    return accessible_domains.filter(id__in=requested_ids), None


def _published_schedule_authority(domain_ids=None):
    published_blocks_query = ScheduleBlock.objects.filter(published_at__isnull=False)
    if domain_ids is not None:
        published_blocks_query = published_blocks_query.filter(domain_id__in=domain_ids)
    published_blocks = list(published_blocks_query.order_by('-published_at', '-id'))
    authoritative_block_by_date = {}
    for block in published_blocks:
        pointer = block.start_date
        while pointer <= block.end_date:
            authoritative_block_by_date.setdefault((block.domain_id, pointer), block.id)
            pointer += timedelta(days=1)
    return published_blocks, authoritative_block_by_date


@api_view(['GET'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def published_schedule(request):
    """Return the assignments from the current published schedule of record."""
    permission = (
        'view_domain_statistics'
        if request.query_params.get('purpose') == 'stats'
        else 'view_published_schedules'
    )
    domains, domain_error = _requested_published_schedule_domains(request, permission)
    if domain_error:
        return domain_error
    domain_ids = list(domains.values_list('id', flat=True))
    published_blocks, authoritative_block_by_date = _published_schedule_authority(domain_ids)

    active_runs = {}
    published_versions = ScheduleVersion.objects.filter(
        schedule_block__in=published_blocks,
    ).select_related('published_optimizer_run')
    for version in published_versions:
        published_run = version.published_optimizer_run
        if published_run is None:
            # Compatibility for legacy published rows before publication
            # snapshots were introduced, and for independently saved V2 runs
            # that were published before the publication fallback was added.
            published_run = version.optimizer_runs.filter(
                status=OptimizerRun.Status.COMPLETED,
            ).order_by('-is_active', '-run_number').first()
        if published_run is not None:
            active_runs[version.id] = published_run
    active_run_ids = [published_run.id for published_run in active_runs.values()]
    published_assignments = (
        ScheduleShiftAssignment.objects.filter(
            shift_instance__schedule_block__in=published_blocks,
        )
        .filter(
        Q(
            assignment_source=ScheduleShiftAssignment.AssignmentSource.MANUAL,
            optimizer_run__isnull=True,
        )
        | Q(optimizer_run_id__in=active_run_ids)
        )
        .order_by()
        .values(
            'id', 'assignment_source', 'optimizer_run_id', 'physician_id',
            'physician__display_name', 'physician__user__first_name',
            'physician__user__last_name', 'physician__user__username',
            'shift_instance__schedule_block_id', 'shift_instance__schedule_version_id',
            'shift_instance__schedule_version__domain_id',
            'shift_instance__schedule_version__domain__name',
            'shift_instance__schedule_version__domain__region_id',
            'shift_instance__schedule_version__domain__region__name',
            'shift_instance__id', 'shift_instance__split_parent_id',
            'shift_instance__split_parent__segment_start_time',
            'shift_instance__split_parent__shift_template__start_time',
            'shift_instance__date', 'shift_instance__facility_id',
            'shift_instance__facility__name', 'shift_instance__facility__short_name',
            'shift_instance__facility__sort_order', 'shift_instance__facility__timezone',
            'shift_instance__shift_template__name', 'shift_instance__start_datetime',
            'shift_instance__end_datetime', 'shift_instance__segment_start_time',
            'shift_instance__segment_end_time', 'posting__mode', 'posting__active',
            'shift_instance__shift_template__start_time',
            'shift_instance__shift_template__end_time',
            'shift_instance__shift_template__night_shift',
            'shift_instance__shift_template_id',
        )
    )
    split_root_ids = {
        assignment['shift_instance__split_parent_id']
        for assignment in published_assignments
        if assignment['shift_instance__split_parent_id'] is not None
    }
    rows = []
    for assignment in published_assignments:
        assignment_date = assignment['shift_instance__date']
        block_id = assignment['shift_instance__schedule_block_id']
        version_id = assignment['shift_instance__schedule_version_id']
        domain_id = assignment['shift_instance__schedule_version__domain_id']
        if authoritative_block_by_date.get((domain_id, assignment_date)) != block_id:
            continue
        active_run = active_runs.get(version_id)
        if active_run is not None and active_run.run_kind in (
            'COPY', 'BENCHMARK', 'OPTIMIZER_V2', 'OPTIMIZER_V2_TEST',
        ):
            if assignment['optimizer_run_id'] != active_run.id:
                continue
        else:
            is_legacy_manual = (
                assignment['assignment_source'] == ScheduleShiftAssignment.AssignmentSource.MANUAL
                and assignment['optimizer_run_id'] is None
            )
            if not is_legacy_manual and (
                active_run is None or assignment['optimizer_run_id'] != active_run.id
            ):
                continue
        physician_name = assignment['physician__display_name'] or ' '.join(
            part for part in (
                assignment['physician__user__first_name'],
                assignment['physician__user__last_name'],
            ) if part
        ) or assignment['physician__user__username']
        start_time = assignment['shift_instance__segment_start_time'] or assignment['shift_instance__shift_template__start_time']
        end_time = assignment['shift_instance__segment_end_time'] or assignment['shift_instance__shift_template__end_time']
        instance_id = assignment['shift_instance__id']
        split_parent_id = assignment['shift_instance__split_parent_id']
        split_group_id = split_parent_id or (instance_id if instance_id in split_root_ids else None)
        split_group_start_time = None
        if split_group_id is not None:
            split_group_start_time = (
                assignment['shift_instance__split_parent__segment_start_time']
                or assignment['shift_instance__split_parent__shift_template__start_time']
                if split_parent_id else start_time
            )
        rows.append({
            'id': assignment['id'],
            'shift_instance_id': instance_id,
            'facility': assignment['shift_instance__facility_id'],
            'facility_name': assignment['shift_instance__facility__name'],
            'facility_short_name': assignment['shift_instance__facility__short_name'],
            'facility_sort_order': assignment['shift_instance__facility__sort_order'],
            'physician': assignment['physician_id'],
            'physician_name': physician_name,
            'role': assignment['shift_instance__shift_template__name'],
            'role_display': assignment['shift_instance__shift_template__name'],
            'date': assignment_date.isoformat(),
            'start_time': start_time.isoformat(),
            'end_time': end_time.isoformat(),
            'is_night': assignment['shift_instance__shift_template__night_shift'],
            'shift_template_id': assignment['shift_instance__shift_template_id'],
            'split_group_id': split_group_id,
            'split_group_start_time': split_group_start_time.isoformat() if split_group_start_time else None,
            'is_split': split_group_id is not None,
            'posting_mode': assignment['posting__mode'] if assignment['posting__active'] else None,
            'status': 'scheduled',
            'status_display': 'Scheduled',
            'schedule_block': block_id,
            'schedule_version': version_id,
            'domain': domain_id,
            'domain_name': assignment['shift_instance__schedule_version__domain__name'],
            'region': assignment['shift_instance__schedule_version__domain__region_id'],
            'region_name': assignment['shift_instance__schedule_version__domain__region__name'],
        })
    open_instances = ScheduleShiftInstance.objects.filter(
        schedule_block__in=published_blocks,
        schedule_version_id__in=active_runs.keys(),
        is_locked_open=True,
    ).select_related(
        'facility', 'shift_template', 'schedule_block',
        'schedule_version__domain__region',
    )
    for instance in open_instances:
        domain = instance.schedule_version.domain
        if authoritative_block_by_date.get((domain.id, instance.date)) != instance.schedule_block_id:
            continue
        start_time = instance.segment_start_time or instance.shift_template.start_time
        end_time = instance.segment_end_time or instance.shift_template.end_time
        rows.append({
            'id': None,
            'shift_instance_id': instance.id,
            'facility': instance.facility_id,
            'facility_name': instance.facility.name,
            'facility_short_name': instance.facility.short_name,
            'facility_sort_order': instance.facility.sort_order,
            'physician': None,
            'physician_name': 'Open',
            'role': instance.shift_template.name,
            'role_display': instance.shift_template.name,
            'date': instance.date.isoformat(),
            'start_time': start_time.isoformat(),
            'end_time': end_time.isoformat(),
            'is_night': instance.shift_template.night_shift,
            'shift_template_id': instance.shift_template_id,
            'split_group_id': None,
            'split_group_start_time': None,
            'is_split': False,
            'posting_mode': None,
            'status': 'open',
            'status_display': 'Open',
            'schedule_block': instance.schedule_block_id,
            'schedule_version': instance.schedule_version_id,
            'domain': domain.id,
            'domain_name': domain.name,
            'region': domain.region_id,
            'region_name': domain.region.name,
        })
    rows.sort(key=lambda row: (
        row['date'], row['facility_sort_order'], row['facility_name'],
        row['start_time'], row['physician_name'], row['id'],
    ))
    return Response(rows)


def _schedule_date_comment_payload(comment):
    return {
        'id': comment.id,
        'source': 'ONE_TIME',
        'series_id': None,
        'date': comment.date.isoformat(),
        'title': comment.title,
        'details': comment.details,
        'schedule_block': comment.schedule_block_id,
        'domain': comment.schedule_block.domain_id,
        'domain_name': comment.schedule_block.domain.name,
        'updated_at': comment.updated_at.isoformat(),
    }


def _monthly_series_date(year, month, weekday, ordinal):
    if ordinal == -1:
        last_day = calendar.monthrange(year, month)[1]
        candidate = date_type(year, month, last_day)
        offset = (int(candidate.strftime('%w')) - weekday) % 7
        return candidate - timedelta(days=offset)
    first = date_type(year, month, 1)
    offset = (weekday - int(first.strftime('%w'))) % 7
    day_number = 1 + offset + (ordinal - 1) * 7
    if day_number > calendar.monthrange(year, month)[1]:
        return None
    return date_type(year, month, day_number)


def _series_occurrence_index(series, candidate):
    if candidate < series.start_date:
        return None
    if series.recurrence_type == ScheduleCommentSeries.RecurrenceType.WEEKLY:
        period = 7 * series.interval
        delta = (candidate - series.start_date).days
        if delta % period:
            return None
        return delta // period + 1
    if int(candidate.strftime('%w')) != series.weekday:
        return None
    expected = _monthly_series_date(
        candidate.year,
        candidate.month,
        series.weekday,
        series.monthly_ordinal,
    )
    if expected != candidate:
        return None
    month_delta = (
        (candidate.year - series.start_date.year) * 12
        + candidate.month - series.start_date.month
    )
    return month_delta + 1


def _series_occurs_on(series, candidate):
    occurrence_index = _series_occurrence_index(series, candidate)
    if occurrence_index is None:
        return False
    if (
        series.end_type == ScheduleCommentSeries.EndType.ON_DATE
        and series.end_date is not None
        and candidate > series.end_date
    ):
        return False
    if (
        series.end_type == ScheduleCommentSeries.EndType.AFTER_COUNT
        and series.occurrence_count is not None
        and occurrence_index > series.occurrence_count
    ):
        return False
    return True


def _schedule_comment_series_payload(series, occurrence_date, exception=None):
    title = exception.title if exception and exception.title is not None else series.title
    details = (
        exception.details
        if exception and exception.details is not None
        else series.details
    )
    return {
        'id': f'series-{series.id}-{occurrence_date.isoformat()}',
        'source': 'RECURRING',
        'series_id': series.id,
        'domain': series.domain_id,
        'date': occurrence_date.isoformat(),
        'title': title,
        'details': details,
        'schedule_block': None,
        'updated_at': series.updated_at.isoformat(),
        'recurrence_type': series.recurrence_type,
        'interval': series.interval,
        'weekday': series.weekday,
        'monthly_ordinal': series.monthly_ordinal,
        'end_type': series.end_type,
        'end_date': series.end_date.isoformat() if series.end_date else None,
        'occurrence_count': series.occurrence_count,
    }


def _parse_comment_date(value):
    try:
        return date_type.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _comment_text(request):
    title = str(request.data.get('title', '')).strip()
    details = str(request.data.get('details', '')).strip()
    if not title:
        return None, None, Response(
            {'detail': 'Enter a comment title.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if len(title) > 100:
        return None, None, Response(
            {'detail': 'Comment titles may contain at most 100 characters.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return title, details, None


def _series_configuration(request, start_date):
    recurrence_type = request.data.get('recurrence_type')
    if recurrence_type not in ScheduleCommentSeries.RecurrenceType.values:
        return None, Response(
            {'detail': 'Choose a valid recurring comment pattern.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        interval = int(request.data.get('interval', 1))
        monthly_ordinal_value = request.data.get('monthly_ordinal')
        monthly_ordinal = (
            int(monthly_ordinal_value)
            if monthly_ordinal_value not in (None, '')
            else None
        )
    except (TypeError, ValueError):
        return None, Response(
            {'detail': 'Enter a valid recurrence interval.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if interval not in (1, 2, 3, 4):
        return None, Response(
            {'detail': 'Weekly comments may repeat every 1 to 4 weeks.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    weekday = int(start_date.strftime('%w'))
    if recurrence_type == ScheduleCommentSeries.RecurrenceType.MONTHLY:
        if monthly_ordinal not in (1, 2, 3, 4, -1):
            return None, Response(
                {'detail': 'Choose first, second, third, fourth, or last weekday.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if _monthly_series_date(
            start_date.year, start_date.month, weekday, monthly_ordinal,
        ) != start_date:
            return None, Response(
                {'detail': 'The selected monthly pattern must include the starting date.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        interval = 1
    else:
        monthly_ordinal = None

    end_type = request.data.get('end_type', ScheduleCommentSeries.EndType.NEVER)
    if end_type not in ScheduleCommentSeries.EndType.values:
        return None, Response(
            {'detail': 'Choose a valid ending option.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    end_date = None
    occurrence_count = None
    if end_type == ScheduleCommentSeries.EndType.ON_DATE:
        end_date = _parse_comment_date(request.data.get('end_date'))
        if end_date is None or end_date < start_date:
            return None, Response(
                {'detail': 'The ending date must be on or after the starting date.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
    elif end_type == ScheduleCommentSeries.EndType.AFTER_COUNT:
        try:
            occurrence_count = int(request.data.get('occurrence_count'))
        except (TypeError, ValueError):
            occurrence_count = 0
        if occurrence_count < 1:
            return None, Response(
                {'detail': 'Enter at least one occurrence.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
    return {
        'recurrence_type': recurrence_type,
        'interval': interval,
        'weekday': weekday,
        'monthly_ordinal': monthly_ordinal,
        'end_type': end_type,
        'end_date': end_date,
        'occurrence_count': occurrence_count,
    }, None


@api_view(['GET', 'POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def published_schedule_comments(request):
    domains, domain_error = _requested_published_schedule_domains(request)
    if domain_error:
        return domain_error
    domain_ids = list(domains.values_list('id', flat=True))
    published_blocks, authoritative_block_by_date = _published_schedule_authority(domain_ids)
    if request.method == 'GET':
        comments_by_key = {}
        authoritative_dates = sorted({key[1] for key in authoritative_block_by_date})
        series_rows = ScheduleCommentSeries.objects.filter(
            Q(domain_id__in=domain_ids) | Q(domain__isnull=True),
        ).prefetch_related('exceptions')
        for series in series_rows:
            exceptions = {row.date: row for row in series.exceptions.all()}
            for candidate in authoritative_dates:
                if not _series_occurs_on(series, candidate):
                    continue
                exception = exceptions.get(candidate)
                if exception and exception.is_cancelled:
                    continue
                comments_by_key[('series', candidate)] = _schedule_comment_series_payload(
                    series,
                    candidate,
                    exception,
                )
        comments = ScheduleDateComment.objects.filter(
            schedule_block__in=published_blocks,
        ).select_related('schedule_block__domain')
        for comment in comments:
            key = (comment.schedule_block.domain_id, comment.date)
            if authoritative_block_by_date.get(key) == comment.schedule_block_id:
                comments_by_key[key] = _schedule_date_comment_payload(comment)
        return Response([
            payload
            for _key, payload in sorted(
                comments_by_key.items(),
                key=lambda item: (item[1]['date'], str(item[0][0])),
            )
        ])

    comment_date = _parse_comment_date(request.data.get('date'))
    raw_domain_id = request.data.get('domain')
    if raw_domain_id in (None, ''):
        candidate_domain_ids = {
            domain_id
            for domain_id, candidate_date in authoritative_block_by_date
            if candidate_date == comment_date
        }
        domain_id = next(iter(candidate_domain_ids)) if len(candidate_domain_ids) == 1 else None
    else:
        try:
            domain_id = int(raw_domain_id)
        except (TypeError, ValueError):
            domain_id = None
    if domain_id is None:
        return Response(
            {'detail': 'Select one Domain before adding a calendar comment.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if domain_id not in domain_ids:
        return Response(
            {'detail': 'You do not have access to the selected Domain.'},
            status=status.HTTP_403_FORBIDDEN,
        )
    domain = get_object_or_404(Domain, id=domain_id)
    if not has_permission(request.user, 'manage_date_comments', domain=domain):
        return Response(
            {'detail': 'Schedule comment management permission is required for this Domain.'},
            status=status.HTTP_403_FORBIDDEN,
        )
    block_id = authoritative_block_by_date.get((domain_id, comment_date))
    if block_id is None:
        return Response(
            {'detail': 'That date is not part of a published schedule.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    title, details, error = _comment_text(request)
    if error:
        return error
    recurrence_type = request.data.get('recurrence_type')
    if recurrence_type:
        configuration, error = _series_configuration(request, comment_date)
        if error:
            return error
        ScheduleDateComment.objects.filter(
            schedule_block_id=block_id,
            date=comment_date,
        ).delete()
        series = ScheduleCommentSeries.objects.create(
            domain=domain,
            title=title,
            details=details,
            start_date=comment_date,
            created_by=request.user,
            updated_by=request.user,
            **configuration,
        )
        return Response(
            _schedule_comment_series_payload(series, comment_date),
            status=status.HTTP_201_CREATED,
        )
    comment, created = ScheduleDateComment.objects.update_or_create(
        schedule_block_id=block_id,
        date=comment_date,
        defaults={
            'title': title,
            'details': details,
            'updated_by': request.user,
        },
    )
    if created:
        comment.created_by = request.user
        comment.save(update_fields=['created_by'])
    return Response(_schedule_date_comment_payload(comment))


@api_view(['PATCH', 'DELETE'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def published_schedule_comment_series_occurrence(request, series_id, comment_date):
    series = get_object_or_404(
        ScheduleCommentSeries.objects.select_related('domain'), id=series_id,
    )
    if series.domain_id is None:
        can_manage_series = is_org_admin(request.user)
    else:
        can_manage_series = has_permission(
            request.user, 'manage_date_comments', domain=series.domain,
        )
    if not can_manage_series:
        return Response(
            {'detail': 'Schedule comment management permission is required for this Domain.'},
            status=status.HTTP_403_FORBIDDEN,
        )
    occurrence_date = _parse_comment_date(comment_date)
    if occurrence_date is None or not _series_occurs_on(series, occurrence_date):
        return Response(
            {'detail': 'That date is not an occurrence of this recurring comment.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    scope = request.data.get('scope', 'THIS')
    if scope not in ('THIS', 'FUTURE', 'ALL'):
        return Response(
            {'detail': 'Choose this date, this and future dates, or the entire series.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if request.method == 'DELETE':
        if scope == 'THIS':
            ScheduleCommentSeriesException.objects.update_or_create(
                series=series,
                date=occurrence_date,
                defaults={'is_cancelled': True, 'updated_by': request.user},
            )
        elif scope == 'ALL' or occurrence_date == series.start_date:
            series.delete()
        else:
            series.end_type = ScheduleCommentSeries.EndType.ON_DATE
            series.end_date = occurrence_date - timedelta(days=1)
            series.occurrence_count = None
            series.updated_by = request.user
            series.save(update_fields=[
                'end_type', 'end_date', 'occurrence_count', 'updated_by', 'updated_at',
            ])
            ScheduleCommentSeriesException.objects.filter(
                series=series,
                date__gte=occurrence_date,
            ).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    title, details, error = _comment_text(request)
    if error:
        return error
    if scope == 'THIS':
        exception, _created = ScheduleCommentSeriesException.objects.update_or_create(
            series=series,
            date=occurrence_date,
            defaults={
                'is_cancelled': False,
                'title': title,
                'details': details,
                'updated_by': request.user,
            },
        )
        return Response(_schedule_comment_series_payload(
            series,
            occurrence_date,
            exception,
        ))
    if scope == 'ALL':
        configuration, error = _series_configuration(request, series.start_date)
        if error:
            return error
        for field, value in configuration.items():
            setattr(series, field, value)
        series.title = title
        series.details = details
        series.updated_by = request.user
        series.save()
        ScheduleCommentSeriesException.objects.filter(
            series=series,
            date=occurrence_date,
        ).delete()
        return Response(_schedule_comment_series_payload(series, occurrence_date))

    occurrence_index = _series_occurrence_index(series, occurrence_date)
    configuration = {
        'recurrence_type': series.recurrence_type,
        'interval': series.interval,
        'weekday': series.weekday,
        'monthly_ordinal': series.monthly_ordinal,
        'end_type': series.end_type,
        'end_date': series.end_date,
        'occurrence_count': series.occurrence_count,
    }
    if (
        configuration['end_type'] == ScheduleCommentSeries.EndType.AFTER_COUNT
        and configuration['occurrence_count'] is not None
    ):
        configuration['occurrence_count'] = max(
            1,
            configuration['occurrence_count'] - occurrence_index + 1,
        )
    with transaction.atomic():
        old_series_id = series.id
        replace_entire_series = occurrence_date == series.start_date
        if not replace_entire_series:
            series.end_type = ScheduleCommentSeries.EndType.ON_DATE
            series.end_date = occurrence_date - timedelta(days=1)
            series.occurrence_count = None
            series.updated_by = request.user
            series.save(update_fields=[
                'end_type', 'end_date', 'occurrence_count', 'updated_by', 'updated_at',
            ])
        new_series = ScheduleCommentSeries.objects.create(
            domain=series.domain,
            title=title,
            details=details,
            start_date=occurrence_date,
            created_by=request.user,
            updated_by=request.user,
            **configuration,
        )
        ScheduleCommentSeriesException.objects.filter(
            series_id=old_series_id,
            date__gte=occurrence_date,
        ).update(series=new_series)
        ScheduleCommentSeriesException.objects.filter(
            series=new_series,
            date=occurrence_date,
        ).delete()
        if replace_entire_series:
            ScheduleCommentSeries.objects.filter(id=old_series_id).delete()
    return Response(_schedule_comment_series_payload(new_series, occurrence_date))


@api_view(['DELETE'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def published_schedule_comment_detail(request, comment_date):
    parsed_date = _parse_comment_date(comment_date)
    domains, domain_error = _requested_published_schedule_domains(request)
    if domain_error:
        return domain_error
    domain_ids = list(domains.values_list('id', flat=True))
    _published_blocks, authoritative_block_by_date = _published_schedule_authority(domain_ids)
    if len(domain_ids) != 1:
        candidate_domain_ids = {
            domain_id
            for domain_id, candidate_date in authoritative_block_by_date
            if candidate_date == parsed_date
        }
        if len(candidate_domain_ids) == 1:
            domain_ids = list(candidate_domain_ids)
    if len(domain_ids) != 1:
        return Response(
            {'detail': 'Select one Domain before deleting a calendar comment.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    domain = get_object_or_404(Domain, id=domain_ids[0])
    if not has_permission(request.user, 'manage_date_comments', domain=domain):
        return Response(
            {'detail': 'Schedule comment management permission is required for this Domain.'},
            status=status.HTTP_403_FORBIDDEN,
        )
    block_id = authoritative_block_by_date.get((domain_ids[0], parsed_date))
    comment = get_object_or_404(
        ScheduleDateComment,
        schedule_block_id=block_id,
        date=parsed_date,
    )
    comment.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


def _published_run_for_version(version):
    """Return the one run represented by a published Schedule Version."""
    return version.published_optimizer_run or version.optimizer_runs.filter(
        status=OptimizerRun.Status.COMPLETED,
    ).order_by('-is_active', '-run_number').first()


def _is_authoritative_published_instance(instance):
    block = instance.schedule_block
    if block.published_at is None:
        return False
    authoritative_block_id = ScheduleBlock.objects.filter(
        domain_id=block.domain_id,
        published_at__isnull=False,
        start_date__lte=instance.date,
        end_date__gte=instance.date,
    ).order_by('-published_at', '-id').values_list('id', flat=True).first()
    return authoritative_block_id == block.id


def _published_assignment_or_404(assignment_id):
    assignment = get_object_or_404(
        ScheduleShiftAssignment.objects.select_related(
            'physician__user', 'shift_instance__facility', 'shift_instance__shift_template',
            'shift_instance__schedule_block', 'shift_instance__schedule_version__domain',
            'shift_instance__schedule_version__published_optimizer_run',
        ), id=assignment_id, shift_instance__schedule_block__published_at__isnull=False,
    )
    published_run = _published_run_for_version(assignment.shift_instance.schedule_version)
    if (
        published_run is None
        or assignment.optimizer_run_id != published_run.id
        or not _is_authoritative_published_instance(assignment.shift_instance)
    ):
        raise Http404
    return assignment


def _published_instance_or_404(instance_id, **filters):
    instance = get_object_or_404(
        ScheduleShiftInstance.objects.select_related(
            'facility', 'schedule_block', 'schedule_version__domain',
            'schedule_version__published_optimizer_run',
        ),
        id=instance_id,
        schedule_block__published_at__isnull=False,
        **filters,
    )
    if (
        _published_run_for_version(instance.schedule_version) is None
        or not _is_authoritative_published_instance(instance)
    ):
        raise Http404
    return instance


def _physician_is_active_in_domain(physician, domain):
    return bool(
        physician.active
        and is_clinically_active(physician.user, domain)
    )


def _schedule_conflict_warning(detail, **extra):
    return Response(
        {
            'detail': detail,
            'requires_confirmation': True,
            **extra,
        },
        status=status.HTTP_409_CONFLICT,
    )


def _force_requested(request):
    return request.data.get('force') is True


@api_view(['GET', 'PATCH'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def shift_trade_policy(request):
    policy = ShiftTradePolicy.load()
    # This remains one application-wide policy. A Domain-scoped approval role
    # must not be able to change behavior in every other Region and Domain.
    can_manage_policy = is_org_admin(request.user)
    if request.method == 'PATCH':
        if not can_manage_policy:
            return Response(
                {'detail': 'Only an Organization Administrator can change the organization-wide shift trade policy.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        value = request.data.get('require_scheduler_approval')
        if type(value) is not bool:
            return Response({'detail': 'Provide require_scheduler_approval as true or false.'}, status=status.HTTP_400_BAD_REQUEST)
        policy.require_scheduler_approval = value
        policy.updated_by = request.user
        policy.save()
    return Response({'require_scheduler_approval': policy.require_scheduler_approval, 'can_manage': can_manage_policy})


@api_view(['POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_assignment_posting(request, assignment_id):
    assignment = _published_assignment_or_404(assignment_id)
    domain = assignment.shift_instance.schedule_version.domain
    owns_shift = assignment.physician.user_id == request.user.id
    permission = 'post_own_shifts' if owns_shift else 'manage_any_shift_posting'
    if not has_permission(request.user, permission, domain=domain):
        return Response({'detail': 'You do not have permission to manage this shift posting.'}, status=status.HTTP_403_FORBIDDEN)
    mode = request.data.get('mode')
    if mode == 'CLOSE':
        ShiftPosting.objects.filter(assignment=assignment).update(active=False)
    elif mode in (ShiftPosting.Mode.PICKUP, ShiftPosting.Mode.TRADE_ONLY):
        ShiftPosting.objects.update_or_create(assignment=assignment, defaults={'mode': mode, 'active': True, 'posted_by': request.user})
    else:
        return Response({'detail': 'Choose pickup, trade only, or close.'}, status=status.HTTP_400_BAD_REQUEST)
    posting = ShiftPosting.objects.filter(assignment=assignment, active=True).first()
    return Response({'assignment_id': assignment.id, 'posting_mode': posting.mode if posting else None})


@api_view(['POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_assignment_split(request, assignment_id):
    assignment = _published_assignment_or_404(assignment_id)
    domain = assignment.shift_instance.schedule_version.domain
    owns_shift = assignment.physician.user_id == request.user.id
    permission = 'split_own_shift' if owns_shift else 'split_any_published_shift'
    if not has_permission(request.user, permission, domain=domain):
        return Response({'detail': 'You do not have permission to split this shift.'}, status=status.HTTP_403_FORBIDDEN)
    try:
        split_clock = datetime.strptime(str(request.data.get('split_time', '')), '%H:%M').time()
    except ValueError:
        return Response({'detail': 'Enter a valid split time.'}, status=status.HTTP_400_BAD_REQUEST)
    instance = assignment.shift_instance
    facility_zone = _timezone_from_name(instance.facility.timezone)
    start_clock = instance.segment_start_time or instance.shift_template.start_time
    end_clock = instance.segment_end_time or instance.shift_template.end_time
    start_at = datetime.combine(instance.date, start_clock, tzinfo=facility_zone)
    end_date = instance.date + timedelta(days=1) if end_clock <= start_clock else instance.date
    end_at = datetime.combine(end_date, end_clock, tzinfo=facility_zone)
    split_date = instance.date + timedelta(days=1) if split_clock <= start_clock and end_date != instance.date else instance.date
    split_at = datetime.combine(split_date, split_clock, tzinfo=facility_zone)
    if not start_at < split_at < end_at:
        return Response({'detail': 'Split time must fall inside the shift.'}, status=status.HTTP_400_BAD_REQUEST)
    with transaction.atomic():
        locked = ScheduleShiftAssignment.objects.select_for_update().select_related('shift_instance').get(id=assignment.id)
        original = locked.shift_instance
        prior_end = end_at
        original.end_datetime = split_at
        original.start_datetime = start_at
        original.segment_start_time = start_clock
        original.segment_end_time = split_clock
        original.save(update_fields=['start_datetime', 'end_datetime', 'segment_start_time', 'segment_end_time', 'updated_at'])
        second = ScheduleShiftInstance.objects.create(
            schedule_version=original.schedule_version, schedule_block=original.schedule_block,
            date=original.date, shift_template=original.shift_template, facility=original.facility,
            start_datetime=split_at, end_datetime=prior_end, required_staffing=original.required_staffing,
            status=original.status, is_locked_open=original.is_locked_open,
            split_parent=original.split_parent or original,
            segment_start_time=split_clock, segment_end_time=end_clock,
        )
        cohort = ScheduleShiftAssignment.objects.filter(
            shift_instance=original,
            optimizer_run=locked.optimizer_run,
        ).select_related('physician')
        ScheduleShiftAssignment.objects.bulk_create([
            ScheduleShiftAssignment(
                shift_instance=second, physician=row.physician, created_by=request.user,
                assignment_source=row.assignment_source, optimizer_run=row.optimizer_run,
                is_locked=row.is_locked,
            )
            for row in cohort
        ])
        ShiftPosting.objects.filter(assignment__shift_instance=original).update(active=False)
    return Response({'detail': 'Shift split successfully.'})


@api_view(['POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_assignment_unsplit(request, assignment_id):
    assignment = _published_assignment_or_404(assignment_id)
    domain = assignment.shift_instance.schedule_version.domain
    can_manage = has_permission(request.user, 'split_any_published_shift', domain=domain)
    owns_shift = assignment.physician.user_id == request.user.id
    if not can_manage and not (
        owns_shift and has_permission(request.user, 'split_own_shift', domain=domain)
    ):
        return Response({'detail': 'You may only unsplit your own shift.'}, status=status.HTTP_403_FORBIDDEN)
    instance = assignment.shift_instance
    root = instance.split_parent or instance
    segments = list(
        ScheduleShiftInstance.objects.filter(Q(id=root.id) | Q(split_parent=root))
        .select_related('shift_template', 'facility')
        .order_by('start_datetime', 'id')
    )
    if len(segments) < 2:
        return Response({'detail': 'This shift is not split.'}, status=status.HTTP_400_BAD_REQUEST)
    segment_ids = [segment.id for segment in segments]
    cohort_filter = {'optimizer_run': assignment.optimizer_run}
    group_assignments = list(
        ScheduleShiftAssignment.objects.filter(
            shift_instance_id__in=segment_ids,
            **cohort_filter,
        ).select_related('physician__user', 'shift_instance')
    )
    physician_sets = [
        {
            row.physician_id
            for row in group_assignments
            if row.shift_instance_id == segment.id
        }
        for segment in segments
    ]
    same_current_owners = bool(physician_sets[0]) and all(
        physicians == physician_sets[0]
        for physicians in physician_sets[1:]
    )
    if not same_current_owners and not can_manage:
        return Response(
            {
                'detail': (
                    'The split portions currently have different scheduled users. '
                    'Contact an administrator or scheduler to recombine this shift.'
                ),
            },
            status=status.HTTP_403_FORBIDDEN,
        )
    selected_physician = None
    if not same_current_owners:
        selected_physician_id = request.data.get('physician_id')
        if selected_physician_id in (None, ''):
            return _schedule_conflict_warning(
                'The split portions have different scheduled users. Choose the user who should receive the recombined shift.',
                requires_physician_selection=True,
            )
        selected_physician = get_object_or_404(
            Physician.objects.select_related('user'),
            id=selected_physician_id,
            active=True,
        )
        if not _physician_is_active_in_domain(selected_physician, domain):
            return Response(
                {'detail': 'The selected user is not clinically active in this Domain.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not _force_requested(request):
            return _schedule_conflict_warning(
                f'The split portions have different scheduled users. Recombine them and assign the full shift to {selected_physician}?',
            )
    template = root.shift_template
    facility_zone = _timezone_from_name(root.facility.timezone)
    start_at = datetime.combine(root.date, template.start_time, tzinfo=facility_zone)
    end_date = root.date + timedelta(days=1) if template.end_time <= template.start_time else root.date
    end_at = datetime.combine(end_date, template.end_time, tzinfo=facility_zone)
    resulting_physician_ids = (
        {selected_physician.id}
        if selected_physician is not None
        else physician_sets[0]
    )
    group_assignment_ids = [row.id for row in group_assignments]
    overlap_names = []
    for physician_id in resulting_physician_ids:
        physician = next(
            (
                row.physician
                for row in group_assignments
                if row.physician_id == physician_id
            ),
            selected_physician,
        )
        overlap = ScheduleShiftAssignment.objects.filter(
            physician_id=physician_id,
            optimizer_run=assignment.optimizer_run,
            shift_instance__schedule_block=instance.schedule_block,
            shift_instance__start_datetime__lt=end_at,
            shift_instance__end_datetime__gt=start_at,
        ).exclude(id__in=group_assignment_ids).exists()
        if overlap:
            overlap_names.append(str(physician))
    if overlap_names:
        warning = (
            'Recombining this shift creates an overlapping assignment for '
            f'{", ".join(overlap_names)}.'
        )
        if not can_manage:
            return Response(
                {'detail': f'{warning} Contact an administrator or scheduler.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not _force_requested(request):
            return _schedule_conflict_warning(f'{warning} Proceed anyway?')

    with transaction.atomic():
        locked_root = ScheduleShiftInstance.objects.select_for_update().get(id=root.id)
        derived_ids = [segment.id for segment in segments if segment.id != root.id]
        root_assignments = list(
            ScheduleShiftAssignment.objects.select_for_update().filter(
                shift_instance=locked_root,
                **cohort_filter,
            )
        )
        root_by_physician = {
            row.physician_id: row
            for row in root_assignments
        }
        if selected_physician is not None:
            surviving_assignment = (
                root_by_physician.get(selected_physician.id)
                or root_assignments[0]
            )
            surviving_assignment.physician = selected_physician
            surviving_assignment.assignment_source = (
                ScheduleShiftAssignment.AssignmentSource.MANUAL
            )
            surviving_assignment.created_by = request.user
            surviving_assignment.is_locked = True
            surviving_assignment.save(update_fields=[
                'physician', 'assignment_source', 'created_by', 'is_locked',
            ])
            replacement_by_physician = {
                row.physician_id: surviving_assignment
                for row in group_assignments
            }
            extra_root_ids = [
                row.id for row in root_assignments
                if row.id != surviving_assignment.id
            ]
        else:
            replacement_by_physician = root_by_physician
            extra_root_ids = []

        now = timezone.now()
        ShiftTrade.objects.filter(
            Q(offered_assignment_id__in=group_assignment_ids)
            | Q(requested_assignment_id__in=group_assignment_ids),
            status__in=[
                ShiftTrade.Status.PENDING_RECIPIENT,
                ShiftTrade.Status.PENDING_SCHEDULER,
            ],
        ).update(status=ShiftTrade.Status.CANCELLED, updated_at=now)
        for row in group_assignments:
            if row.shift_instance_id == locked_root.id and row.id not in extra_root_ids:
                continue
            replacement = replacement_by_physician.get(row.physician_id)
            if replacement is None and selected_physician is not None:
                replacement = surviving_assignment
            if replacement is None:
                continue
            ShiftTrade.objects.filter(offered_assignment=row).update(
                offered_assignment=replacement,
                updated_at=now,
            )
            ShiftTrade.objects.filter(requested_assignment=row).update(
                requested_assignment=replacement,
                updated_at=now,
            )
        ShiftPosting.objects.filter(assignment__shift_instance_id__in=segment_ids).delete()
        if extra_root_ids:
            ScheduleShiftAssignment.objects.filter(id__in=extra_root_ids).delete()
        ScheduleShiftInstance.objects.filter(id__in=derived_ids).delete()
        locked_root.start_datetime = start_at
        locked_root.end_datetime = end_at
        locked_root.segment_start_time = None
        locked_root.segment_end_time = None
        locked_root.save(update_fields=['start_datetime', 'end_datetime', 'segment_start_time', 'segment_end_time', 'updated_at'])
    return Response({
        'detail': 'Shift recombined successfully.',
        'cancelled_active_trades': True,
    })


@api_view(['POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_assignment_reassign(request, assignment_id):
    assignment = _published_assignment_or_404(assignment_id)
    domain = assignment.shift_instance.schedule_version.domain
    if not has_permission(request.user, 'manage_published_assignments', domain=domain):
        return Response({'detail': 'Only a scheduler or administrator can change the scheduled user.'}, status=status.HTTP_403_FORBIDDEN)
    physician = get_object_or_404(
        Physician.objects.select_related('user'),
        id=request.data.get('physician_id'),
        active=True,
    )
    if not _physician_is_active_in_domain(physician, domain):
        return Response(
            {'detail': 'The selected user is not clinically active in this Domain.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    valid, reason = _trade_assignment_is_valid(physician, assignment, [assignment.id])
    if not valid and not _force_requested(request):
        return _schedule_conflict_warning(f'{reason} Proceed anyway?')
    assignment.physician = physician
    assignment.assignment_source = ScheduleShiftAssignment.AssignmentSource.MANUAL
    assignment.created_by = request.user
    assignment.is_locked = True
    assignment.save()
    ShiftPosting.objects.filter(assignment=assignment).update(active=False)
    return Response({'detail': 'Scheduled user changed.'})


@api_view(['POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_assignment_open(request, assignment_id):
    """Remove one published assignment while retaining its trade audit history."""
    assignment = _published_assignment_or_404(assignment_id)
    domain = assignment.shift_instance.schedule_version.domain
    if not has_permission(request.user, 'manage_published_assignments', domain=domain):
        return Response(
            {'detail': 'Only a scheduler or administrator can open a scheduled shift.'},
            status=status.HTTP_403_FORBIDDEN,
        )
    instance = assignment.shift_instance
    now = timezone.now()
    with transaction.atomic():
        locked_assignment = (
            ScheduleShiftAssignment.objects.select_for_update()
            .select_related(
                'physician__user', 'shift_instance__facility',
                'shift_instance__shift_template',
            )
            .get(id=assignment.id)
        )
        snapshot = _trade_assignment_payload(locked_assignment)
        offered_trades = ShiftTrade.objects.filter(
            offered_assignment=locked_assignment,
        )
        requested_trades = ShiftTrade.objects.filter(
            requested_assignment=locked_assignment,
        )
        offered_trades.filter(offered_assignment_snapshot={}).update(
            offered_assignment_snapshot=snapshot,
        )
        requested_trades.filter(requested_assignment_snapshot={}).update(
            requested_assignment_snapshot=snapshot,
        )
        ShiftTrade.objects.filter(
            Q(offered_assignment=locked_assignment)
            | Q(requested_assignment=locked_assignment),
            status__in=[
                ShiftTrade.Status.PENDING_RECIPIENT,
                ShiftTrade.Status.PENDING_SCHEDULER,
            ],
        ).update(status=ShiftTrade.Status.CANCELLED, updated_at=now)
        ShiftPosting.objects.filter(assignment=locked_assignment).delete()
        locked_assignment.delete()
        locked_instance = ScheduleShiftInstance.objects.select_for_update().get(
            id=instance.id,
        )
        locked_instance.is_locked_open = True
        locked_instance.status = ScheduleShiftInstance.Status.OPEN
        locked_instance.save(update_fields=['is_locked_open', 'status', 'updated_at'])
        _set_active_run_locked_open(locked_instance, True)
    return Response({'detail': 'Shift opened.'})


@api_view(['POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def shift_instance_assign(request, instance_id):
    """Assign an explicitly open published shift to a selected user."""
    instance = _published_instance_or_404(instance_id, is_locked_open=True)
    if not has_permission(
        request.user, 'manage_published_assignments', domain=instance.schedule_version.domain,
    ):
        return Response(
            {'detail': 'Only a scheduler or administrator can fill an open shift.'},
            status=status.HTTP_403_FORBIDDEN,
        )
    physician = get_object_or_404(
        Physician.objects.select_related('user'),
        id=request.data.get('physician_id'),
        active=True,
    )
    if not _physician_is_active_in_domain(
        physician, instance.schedule_version.domain,
    ):
        return Response(
            {'detail': 'The selected user is not clinically active in this Domain.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    active_run = _published_run_for_version(instance.schedule_version)
    if active_run is None:
        return Response(
            {'detail': 'This published schedule does not have an active run.'},
            status=status.HTTP_409_CONFLICT,
        )
    if ScheduleShiftAssignment.objects.filter(
        shift_instance=instance,
        optimizer_run=active_run,
        physician=physician,
    ).exists():
        return Response(
            {'detail': 'That user is already assigned to this shift.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    overlap = ScheduleShiftAssignment.objects.filter(
        physician=physician,
        optimizer_run=active_run,
        shift_instance__schedule_block=instance.schedule_block,
        shift_instance__start_datetime__lt=instance.end_datetime,
        shift_instance__end_datetime__gt=instance.start_datetime,
    ).exclude(shift_instance=instance).exists()
    if overlap and not _force_requested(request):
        return _schedule_conflict_warning(
            f'{physician} has an overlapping assignment. Proceed anyway?'
        )
    with transaction.atomic():
        locked_instance = ScheduleShiftInstance.objects.select_for_update().get(
            id=instance.id,
        )
        assigned_count = ScheduleShiftAssignment.objects.filter(
            shift_instance=locked_instance,
            optimizer_run=active_run,
        ).count()
        if assigned_count >= locked_instance.required_staffing:
            return Response(
                {'detail': 'This shift no longer has an open position.'},
                status=status.HTTP_409_CONFLICT,
            )
        ScheduleShiftAssignment.objects.create(
            shift_instance=locked_instance,
            physician=physician,
            assignment_source=ScheduleShiftAssignment.AssignmentSource.MANUAL,
            optimizer_run=active_run,
            created_by=request.user,
            is_locked=True,
        )
        remains_open = assigned_count + 1 < locked_instance.required_staffing
        locked_instance.is_locked_open = remains_open
        locked_instance.status = (
            ScheduleShiftInstance.Status.OPEN
            if remains_open
            else ScheduleShiftInstance.Status.ASSIGNED
        )
        locked_instance.save(update_fields=['is_locked_open', 'status', 'updated_at'])
        _set_active_run_locked_open(locked_instance, remains_open)
    return Response({'detail': 'Open shift assigned.'})


@api_view(['PATCH'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def shift_instance_times(request, instance_id):
    """Change one published shift instance without altering its recurring template."""
    instance = _published_instance_or_404(instance_id)
    domain = instance.schedule_version.domain
    owns_shift = ScheduleShiftAssignment.objects.filter(
        shift_instance=instance,
        optimizer_run=_published_run_for_version(instance.schedule_version),
        physician__user=request.user,
    ).exists()
    if not (
        has_permission(request.user, 'modify_any_published_shift_times', domain=domain)
        or (
            owns_shift
            and has_permission(request.user, 'modify_own_shift_times', domain=domain)
        )
    ):
        return Response(
            {'detail': 'You do not have permission to change this shift’s actual times.'},
            status=status.HTTP_403_FORBIDDEN,
        )
    try:
        start_clock = datetime.strptime(str(request.data.get('start_time', '')), '%H:%M').time()
        end_clock = datetime.strptime(str(request.data.get('end_time', '')), '%H:%M').time()
    except ValueError:
        return Response({'detail': 'Enter valid start and end times.'}, status=status.HTTP_400_BAD_REQUEST)
    if start_clock == end_clock:
        return Response({'detail': 'Start and end times must be different.'}, status=status.HTTP_400_BAD_REQUEST)

    facility_zone = _timezone_from_name(instance.facility.timezone)
    start_at = datetime.combine(instance.date, start_clock, tzinfo=facility_zone)
    end_date = instance.date + timedelta(days=1) if end_clock <= start_clock else instance.date
    end_at = datetime.combine(end_date, end_clock, tzinfo=facility_zone)
    active_run = _published_run_for_version(instance.schedule_version)
    instance_assignments = list(
        ScheduleShiftAssignment.objects.filter(
            shift_instance=instance,
            optimizer_run=active_run,
        ).select_related('physician')
    )
    overlap_names = []
    for row in instance_assignments:
        overlap = ScheduleShiftAssignment.objects.filter(
            physician=row.physician,
            optimizer_run=active_run,
            shift_instance__schedule_block=instance.schedule_block,
            shift_instance__start_datetime__lt=end_at,
            shift_instance__end_datetime__gt=start_at,
        ).exclude(shift_instance=instance).exists()
        if overlap:
            overlap_names.append(str(row.physician))
    if overlap_names and not _force_requested(request):
        return _schedule_conflict_warning(
            'These times create an overlapping assignment for '
            f'{", ".join(sorted(set(overlap_names)))}. Proceed anyway?'
        )
    instance.start_datetime = start_at
    instance.end_datetime = end_at
    instance.segment_start_time = start_clock
    instance.segment_end_time = end_clock
    try:
        instance.save(update_fields=[
            'start_datetime', 'end_datetime', 'segment_start_time', 'segment_end_time', 'updated_at',
        ])
    except IntegrityError:
        return Response(
            {'detail': 'Another instance of this shift already starts at that time.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return Response({
        'detail': 'Actual shift times updated.', 'shift_instance_id': instance.id,
        'start_time': start_clock.isoformat(), 'end_time': end_clock.isoformat(),
    })


def _trade_assignment_payload(assignment, snapshot=None):
    if assignment is None:
        return snapshot or None
    instance = assignment.shift_instance
    physician = assignment.physician
    return {
        'id': assignment.id,
        'physician_id': physician.id,
        'physician_name': physician.display_name or physician.user.get_full_name() or physician.user.username,
        'date': instance.date.isoformat(),
        'facility': instance.facility.short_name,
        'shift': instance.shift_template.name,
        'start_time': instance.start_datetime.isoformat(),
        'end_time': instance.end_datetime.isoformat(),
    }


def _trade_payload(trade, user):
    domain_assignment = trade.offered_assignment or trade.requested_assignment
    trade_domain = trade.domain or (
        domain_assignment.shift_instance.schedule_version.domain
        if domain_assignment is not None else None
    )
    return {
        'id': trade.id,
        'status': trade.status,
        'status_display': trade.get_status_display(),
        'note': trade.note,
        'trade_type': trade.trade_type,
        'requester_id': trade.requester_id,
        'recipient_id': trade.recipient_id,
        'offered_assignment': _trade_assignment_payload(
            trade.offered_assignment,
            trade.offered_assignment_snapshot,
        ),
        'requested_assignment': _trade_assignment_payload(
            trade.requested_assignment,
            trade.requested_assignment_snapshot,
        ),
        'created_at': trade.created_at.isoformat(),
        'can_accept': trade.status == ShiftTrade.Status.PENDING_RECIPIENT and trade.recipient and trade.recipient.user_id == user.id,
        'can_cancel': trade.status in (ShiftTrade.Status.PENDING_RECIPIENT, ShiftTrade.Status.PENDING_SCHEDULER) and trade.requester.user_id == user.id,
        'can_review': (
            trade.status == ShiftTrade.Status.PENDING_SCHEDULER
            and trade_domain is not None
            and has_permission(user, 'approve_pickups_trades', domain=trade_domain)
        ),
    }


def _trade_queryset():
    return ShiftTrade.objects.select_related(
        'domain',
        'requester__user', 'recipient__user',
        'offered_assignment__physician__user',
        'offered_assignment__shift_instance__facility',
        'offered_assignment__shift_instance__shift_template',
        'offered_assignment__shift_instance__schedule_block',
        'offered_assignment__shift_instance__schedule_version__domain',
        'requested_assignment__physician__user',
        'requested_assignment__shift_instance__facility',
        'requested_assignment__shift_instance__shift_template',
        'requested_assignment__shift_instance__schedule_block',
        'requested_assignment__shift_instance__schedule_version__domain',
    )


@api_view(['GET', 'POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def shift_trades(request):
    physician = getattr(request.user, 'physician', None)
    managed_domain_ids = (
        permitted_domain_ids(request.user, 'manage_user_offers_trades')
        | permitted_domain_ids(request.user, 'approve_pickups_trades')
    )
    if request.method == 'GET':
        trades = _trade_queryset()
        access_filter = Q(domain_id__in=managed_domain_ids)
        if physician is not None:
            access_filter |= Q(requester=physician) | Q(recipient=physician)
        trades = trades.filter(access_filter).distinct()
        return Response([_trade_payload(trade, request.user) for trade in trades[:200]])

    if physician is None:
        return Response({'detail': 'Your user account is not linked to a physician.'}, status=status.HTTP_403_FORBIDDEN)
    target = _published_assignment_or_404(request.data.get('target_assignment_id'))
    domain = target.shift_instance.schedule_version.domain
    posting = ShiftPosting.objects.filter(assignment=target, active=True).first()
    offered_id = request.data.get('offered_assignment_id')
    if target.physician_id == physician.id:
        return Response({'detail': 'This shift is not available to you.'}, status=status.HTTP_400_BAD_REQUEST)
    offered = None
    trade_type = ShiftTrade.TradeType.PICKUP
    if offered_id:
        if not has_permission(request.user, 'propose_trade', domain=domain):
            return Response({'detail': 'You do not have permission to propose a trade in this Domain.'}, status=status.HTTP_403_FORBIDDEN)
        offered = _published_assignment_or_404(offered_id)
        trade_type = ShiftTrade.TradeType.TRADE
        if offered.physician_id != physician.id:
            return Response({'detail': 'You may only offer one of your own shifts.'}, status=status.HTTP_403_FORBIDDEN)
        if offered.shift_instance.schedule_block_id != target.shift_instance.schedule_block_id:
            return Response({'detail': 'Both shifts must belong to the same schedule block.'}, status=status.HTTP_400_BAD_REQUEST)
        valid_option_ids = {
            option['id'] for option in _trade_options_for_assignment(offered)
        }
        if target.id not in valid_option_ids:
            return Response({'detail': 'That shift is not a valid trade option for your current schedule.'}, status=status.HTTP_400_BAD_REQUEST)
    elif not has_permission(request.user, 'pick_up_shifts', domain=domain):
        return Response({'detail': 'You do not have permission to pick up shifts in this Domain.'}, status=status.HTTP_403_FORBIDDEN)
    elif not posting or posting.mode != ShiftPosting.Mode.PICKUP:
        return Response({'detail': 'This shift is not posted for pickup.'}, status=status.HTTP_400_BAD_REQUEST)
    if ShiftTrade.objects.filter(
        Q(offered_assignment=target) | Q(requested_assignment=target),
        status__in=[ShiftTrade.Status.PENDING_RECIPIENT, ShiftTrade.Status.PENDING_SCHEDULER],
    ).exists():
        return Response({'detail': 'This shift already has a pending request.'}, status=status.HTTP_400_BAD_REQUEST)
    trade = ShiftTrade.objects.create(
        domain=domain,
        offered_assignment=target, requested_assignment=offered,
        offered_assignment_snapshot=_trade_assignment_payload(target),
        requested_assignment_snapshot=_trade_assignment_payload(offered) or {},
        requester=physician, recipient=target.physician, trade_type=trade_type,
        note=str(request.data.get('note', '')).strip(),
    )
    return Response(_trade_payload(_trade_queryset().get(id=trade.id), request.user), status=status.HTTP_201_CREATED)


def _trade_options_for_assignment(offered, allow_conflicts=False):
    proposer = offered.physician
    run = offered.optimizer_run
    block = offered.shift_instance.schedule_block
    cohort = ScheduleShiftAssignment.objects.filter(
        shift_instance__schedule_block=block,
        optimizer_run=run,
        physician__active=True,
    )
    cohort_rows = list(cohort.order_by(
        'physician__display_name', 'shift_instance__date',
        'shift_instance__facility__sort_order', 'shift_instance__start_datetime', 'id',
    ).values(
        'id', 'physician_id', 'physician__display_name',
        'physician__user__first_name', 'physician__user__last_name', 'physician__user__username',
        'shift_instance__date', 'shift_instance__facility_id', 'shift_instance__facility__short_name',
        'shift_instance__segment_start_time', 'shift_instance__segment_end_time',
        'shift_instance__shift_template__start_time', 'shift_instance__shift_template__end_time',
    ))
    proposer_work_dates = {
        row['shift_instance__date'] for row in cohort_rows if row['physician_id'] == proposer.id
    }
    physician_dates = {}
    for row in cohort_rows:
        physician_dates.setdefault(row['physician_id'], set()).add(row['shift_instance__date'])
    options = []
    for target in cohort_rows:
        if target['physician_id'] == proposer.id:
            continue
        target_date = target['shift_instance__date']
        if not allow_conflicts and target_date in proposer_work_dates:
            continue
        if (
            not allow_conflicts
            and offered.shift_instance.date
            in physician_dates.get(target['physician_id'], set())
        ):
            continue
        start_time = target['shift_instance__segment_start_time'] or target['shift_instance__shift_template__start_time']
        end_time = target['shift_instance__segment_end_time'] or target['shift_instance__shift_template__end_time']
        physician_name = target['physician__display_name'] or ' '.join(
            part for part in (
                target['physician__user__first_name'], target['physician__user__last_name'],
            ) if part
        ) or target['physician__user__username']
        options.append({
            'id': target['id'],
            'physician_id': target['physician_id'],
            'physician_name': physician_name,
            'date': target_date.isoformat(),
            'facility': target['shift_instance__facility__short_name'],
            'start_time': start_time.isoformat(),
            'end_time': end_time.isoformat(),
        })
    return options


@api_view(['GET'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_assignment_trade_options(request, assignment_id):
    offered = _published_assignment_or_404(assignment_id)
    domain = offered.shift_instance.schedule_version.domain
    can_manage = has_permission(request.user, 'manage_user_offers_trades', domain=domain)
    if offered.physician.user_id != request.user.id and not can_manage:
        return Response({'detail': 'You may only propose a trade from your own shift.'}, status=status.HTTP_403_FORBIDDEN)
    if offered.physician.user_id == request.user.id and not has_permission(
        request.user, 'propose_trade', domain=domain,
    ):
        return Response({'detail': 'You do not have permission to propose a trade in this Domain.'}, status=status.HTTP_403_FORBIDDEN)
    return Response(_trade_options_for_assignment(
        offered,
        allow_conflicts=can_manage,
    ))


@api_view(['POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_assignment_swap(request, assignment_id):
    source = _published_assignment_or_404(assignment_id)
    domain = source.shift_instance.schedule_version.domain
    if not has_permission(request.user, 'manage_published_assignments', domain=domain):
        return Response(
            {'detail': 'Only a scheduler or administrator can directly swap scheduled users.'},
            status=status.HTTP_403_FORBIDDEN,
        )
    target = _published_assignment_or_404(request.data.get('target_assignment_id'))
    if source.id == target.id or source.physician_id == target.physician_id:
        return Response(
            {'detail': 'Choose a shift assigned to a different user.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if (
        source.shift_instance.schedule_block_id
        != target.shift_instance.schedule_block_id
        or source.optimizer_run_id != target.optimizer_run_id
    ):
        return Response(
            {'detail': 'Both shifts must belong to the same published schedule.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    excluded = [source.id, target.id]
    warnings = []
    valid, reason = _trade_assignment_is_valid(
        target.physician,
        source,
        excluded,
    )
    if not valid:
        warnings.append(reason)
    valid, reason = _trade_assignment_is_valid(
        source.physician,
        target,
        excluded,
    )
    if not valid:
        warnings.append(reason)
    if warnings and not _force_requested(request):
        return _schedule_conflict_warning(
            f'{" ".join(warnings)} Proceed anyway?'
        )

    now = timezone.now()
    with transaction.atomic():
        locked = {
            row.id: row
            for row in ScheduleShiftAssignment.objects.select_for_update().filter(
                id__in=excluded,
            ).select_related('physician')
        }
        locked_source = locked[source.id]
        locked_target = locked[target.id]
        source_physician = locked_source.physician
        target_physician = locked_target.physician
        ShiftTrade.objects.filter(
            Q(offered_assignment_id__in=excluded)
            | Q(requested_assignment_id__in=excluded),
            status__in=[
                ShiftTrade.Status.PENDING_RECIPIENT,
                ShiftTrade.Status.PENDING_SCHEDULER,
            ],
        ).update(status=ShiftTrade.Status.CANCELLED, updated_at=now)
        ShiftPosting.objects.filter(assignment_id__in=excluded).update(
            active=False,
            updated_at=now,
        )
        locked_source.physician = target_physician
        locked_target.physician = source_physician
        for row in (locked_source, locked_target):
            row.assignment_source = ScheduleShiftAssignment.AssignmentSource.MANUAL
            row.created_by = request.user
            row.is_locked = True
            row.save(update_fields=[
                'physician', 'assignment_source', 'created_by', 'is_locked',
            ])
        ShiftTrade.objects.create(
            domain=domain,
            offered_assignment=locked_target,
            requested_assignment=locked_source,
            offered_assignment_snapshot=_trade_assignment_payload(locked_target),
            requested_assignment_snapshot=_trade_assignment_payload(locked_source),
            requester=source_physician,
            recipient=target_physician,
            trade_type=ShiftTrade.TradeType.TRADE,
            status=ShiftTrade.Status.APPROVED,
            responded_at=now,
            reviewed_at=now,
            reviewed_by=request.user,
            note='Direct scheduler/admin swap.',
        )
    return Response({'detail': 'Scheduled users swapped.'})


def _trade_assignment_is_valid(physician, assignment, excluded_ids):
    instance = assignment.shift_instance
    if not physician.active:
        return False, f'{physician} is not an active physician.'
    domain = instance.schedule_version.domain
    if not _physician_is_active_in_domain(physician, domain):
        return False, f'{physician} is not clinically active in {domain.name}.'
    overlap = ScheduleShiftAssignment.objects.filter(
        physician=physician,
        optimizer_run=assignment.optimizer_run,
        shift_instance__schedule_block=instance.schedule_block,
        shift_instance__start_datetime__lt=instance.end_datetime,
        shift_instance__end_datetime__gt=instance.start_datetime,
    ).exclude(id__in=excluded_ids).exists()
    if overlap:
        return False, f'{physician} has an overlapping assignment.'
    return True, ''


def _apply_shift_trade(trade, reviewed_by=None, force=False):
    now = timezone.now()
    with transaction.atomic():
        target = ScheduleShiftAssignment.objects.select_for_update().select_related(
            'physician__user', 'shift_instance__schedule_block',
            'shift_instance__schedule_version__domain',
            'shift_instance__facility',
        ).get(id=trade.offered_assignment_id)
        offered = None
        if trade.requested_assignment_id:
            offered = ScheduleShiftAssignment.objects.select_for_update().select_related(
                'physician__user', 'shift_instance__schedule_block',
                'shift_instance__schedule_version__domain',
                'shift_instance__facility',
            ).get(id=trade.requested_assignment_id)
        assignments = [target] + ([offered] if offered else [])
        if any(
            assignment.optimizer_run_id
            != getattr(
                _published_run_for_version(assignment.shift_instance.schedule_version),
                'id',
                None,
            )
            or not _is_authoritative_published_instance(assignment.shift_instance)
            for assignment in assignments
        ):
            return False, 'This request no longer belongs to the current published schedule.'
        if target.physician_id != trade.recipient_id or (offered and offered.physician_id != trade.requester_id):
            return False, 'An assignment changed after this request was created.'
        excluded = [target.id] + ([offered.id] if offered else [])
        valid, reason = _trade_assignment_is_valid(trade.requester, target, excluded)
        if not valid and not force:
            return False, reason
        if offered:
            valid, reason = _trade_assignment_is_valid(trade.recipient, offered, excluded)
            if not valid and not force:
                return False, reason
        target.physician = trade.requester
        target.assignment_source = ScheduleShiftAssignment.AssignmentSource.MANUAL
        target.created_by, target.is_locked = reviewed_by, True
        target.save()
        if offered:
            offered.physician = trade.recipient
            offered.assignment_source = ScheduleShiftAssignment.AssignmentSource.MANUAL
            offered.created_by, offered.is_locked = reviewed_by, True
            offered.save()
        ShiftPosting.objects.filter(assignment=target).update(active=False)
        trade.status = ShiftTrade.Status.APPROVED
        trade.reviewed_at, trade.reviewed_by = now, reviewed_by
        trade.save(update_fields=['status', 'reviewed_at', 'reviewed_by', 'updated_at'])
        _cancel_competing_trades(trade)
    return True, ''


def _cancel_competing_trades(trade):
    assignment_ids = [trade.offered_assignment_id]
    if trade.requested_assignment_id:
        assignment_ids.append(trade.requested_assignment_id)
    ShiftTrade.objects.filter(
        Q(offered_assignment_id__in=assignment_ids) | Q(requested_assignment_id__in=assignment_ids),
        status__in=[ShiftTrade.Status.PENDING_RECIPIENT, ShiftTrade.Status.PENDING_SCHEDULER],
    ).exclude(id=trade.id).update(status=ShiftTrade.Status.CANCELLED, updated_at=timezone.now())


@api_view(['POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def shift_trade_action(request, trade_id, action):
    trade = get_object_or_404(_trade_queryset(), id=trade_id)
    now = timezone.now()
    if action in ('accept', 'decline'):
        if trade.recipient.user_id != request.user.id or trade.status != ShiftTrade.Status.PENDING_RECIPIENT:
            return Response({'detail': 'This trade is not awaiting your response.'}, status=status.HTTP_403_FORBIDDEN)
        requires_review = ShiftTradePolicy.load().require_scheduler_approval
        trade.status = ShiftTrade.Status.PENDING_SCHEDULER if action == 'accept' else ShiftTrade.Status.DECLINED
        trade.responded_at = now
        trade.save(update_fields=['status', 'responded_at', 'updated_at'])
        if action == 'accept':
            _cancel_competing_trades(trade)
        if action == 'accept' and not requires_review:
            applied, reason = _apply_shift_trade(trade, reviewed_by=request.user)
            if not applied:
                return Response({'detail': reason}, status=status.HTTP_400_BAD_REQUEST)
    elif action == 'cancel':
        if trade.requester.user_id != request.user.id or trade.status not in (ShiftTrade.Status.PENDING_RECIPIENT, ShiftTrade.Status.PENDING_SCHEDULER):
            return Response({'detail': 'This trade cannot be cancelled.'}, status=status.HTTP_403_FORBIDDEN)
        trade.status = ShiftTrade.Status.CANCELLED
        trade.save(update_fields=['status', 'updated_at'])
    elif action in ('approve', 'reject'):
        domain = trade.domain or trade.offered_assignment.shift_instance.schedule_version.domain
        if not has_permission(
            request.user, 'approve_pickups_trades', domain=domain,
        ) or trade.status != ShiftTrade.Status.PENDING_SCHEDULER:
            return Response({'detail': 'This trade is not awaiting scheduler review.'}, status=status.HTTP_403_FORBIDDEN)
        if action == 'approve':
            applied, reason = _apply_shift_trade(
                trade,
                reviewed_by=request.user,
                force=_force_requested(request),
            )
            if not applied:
                return _schedule_conflict_warning(f'{reason} Proceed anyway?')
        else:
            trade.status = ShiftTrade.Status.DECLINED
            trade.reviewed_at, trade.reviewed_by = now, request.user
            trade.save(update_fields=['status', 'reviewed_at', 'reviewed_by', 'updated_at'])
    else:
        return Response({'detail': 'Unknown trade action.'}, status=status.HTTP_404_NOT_FOUND)
    return Response(_trade_payload(_trade_queryset().get(id=trade.id), request.user))


@api_view(['GET', 'PUT', 'PATCH', 'DELETE'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def shift_detail(request, shift_id):
    shift = get_object_or_404(
        Shift.objects.select_related(
            'facility__region__organization', 'physician', 'physician__user',
        ),
        id=shift_id,
    )

    owns_shift = shift.physician.user_id == request.user.id
    can_administer = is_org_admin(
        request.user,
        shift.facility.region.organization,
    )

    if request.method == 'GET':
        if not owns_shift and not can_administer:
            return Response(
                {'detail': 'You do not have permission to view this legacy Shift.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = ShiftSerializer(shift)
        return Response(serializer.data)

    if not can_administer:
        return Response(
            {'detail': 'Organization Administrator permission is required to modify a legacy Shift.'},
            status=status.HTTP_403_FORBIDDEN,
        )

    if request.method in ['PUT', 'PATCH']:
        partial = request.method == 'PATCH'
        serializer = ShiftSerializer(shift, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        target_facility = serializer.validated_data.get('facility', shift.facility)
        target_physician = serializer.validated_data.get('physician', shift.physician)
        if not is_org_admin(request.user, target_facility.region.organization):
            return Response(
                {'detail': 'You cannot move a legacy Shift into another organization.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        if not OrganizationMembership.objects.filter(
            organization=target_facility.region.organization,
            user=target_physician.user,
            active=True,
        ).exists():
            return Response(
                {'detail': 'The selected user does not belong to this Facility organization.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer.save()
        return Response(serializer.data)

    shift.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['GET', 'POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def shift_templates_list_create(request):
    if request.method == 'GET':
        requested_permission = request.query_params.get(
            'permission', 'manage_shift_templates',
        )
        if requested_permission not in {
            'manage_shift_templates',
            'manage_build_workspace',
            'view_domain_statistics',
        }:
            return Response(
                {'permission': ['This permission cannot be used to view Shift Templates.']},
                status=status.HTTP_400_BAD_REQUEST,
            )
        allowed_domain_ids = permitted_domain_ids(
            request.user, requested_permission,
        )
        if not allowed_domain_ids:
            return Response(
                {'detail': 'Shift Template access is required.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        templates = _ordered_shift_templates().filter(domain_id__in=allowed_domain_ids)

        facility_id = request.query_params.get('facility')
        domain_id = request.query_params.get('domain')
        region_id = request.query_params.get('region')
        active_filter = request.query_params.get('active')
        search = request.query_params.get('search')

        if facility_id:
            templates = templates.filter(facility_id=facility_id)
        if domain_id:
            templates = templates.filter(domain_id=domain_id)
        elif region_id:
            templates = templates.filter(domain__region_id=region_id)

        if active_filter in {'true', 'false'}:
            templates = templates.filter(active=active_filter == 'true')

        if search:
            templates = templates.filter(
                Q(name__icontains=search)
                | Q(facility__name__icontains=search)
            )

        serializer = ShiftTemplateSerializer(templates, many=True)
        return Response(serializer.data)

    serializer = ShiftTemplateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    domain = serializer.validated_data['domain']
    if not has_permission(request.user, 'manage_shift_templates', domain=domain):
        return Response(
            {'detail': 'Shift template management permission is required for this Domain.'},
            status=status.HTTP_403_FORBIDDEN,
        )
    serializer.save()
    return Response(serializer.data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT', 'PATCH'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def shift_template_detail(request, template_id):
    template = get_object_or_404(
        ShiftTemplate.objects.select_related('facility', 'domain__region'),
        id=template_id,
    )

    if not has_permission(request.user, 'manage_shift_templates', domain=template.domain):
        return Response(
            {'detail': 'Shift template management permission is required for this Domain.'},
            status=status.HTTP_403_FORBIDDEN,
        )

    if request.method == 'GET':
        serializer = ShiftTemplateSerializer(template)
        return Response(serializer.data)

    partial = request.method == 'PATCH'
    serializer = ShiftTemplateSerializer(template, data=request.data, partial=partial)
    serializer.is_valid(raise_exception=True)
    target_domain = serializer.validated_data.get('domain', template.domain)
    if not has_permission(request.user, 'manage_shift_templates', domain=target_domain):
        return Response(
            {'detail': 'Shift templates cannot be moved to a Domain you do not manage.'},
            status=status.HTTP_403_FORBIDDEN,
        )
    serializer.save()
    return Response(serializer.data)


def _stats_group_payload(group, allowed_domain_ids=None):
    templates = group.shift_templates.order_by('facility__sort_order', 'start_time', 'id')
    if allowed_domain_ids is not None:
        templates = templates.filter(domain_id__in=allowed_domain_ids)
    return {
        'id': group.id,
        'name': group.name,
        'shift_template_ids': list(templates.values_list('id', flat=True)),
        'domain_ids': list(templates.order_by().values_list('domain_id', flat=True).distinct()),
    }


def _validate_stats_group_data(data, instance=None):
    name = str(data.get('name', instance.name if instance else '')).strip()
    raw_ids = data.get('shift_template_ids')
    if raw_ids is None and instance is not None:
        template_ids = list(instance.shift_templates.values_list('id', flat=True))
    elif not isinstance(raw_ids, list):
        return None, None, 'Select one or more shifts.'
    else:
        try:
            template_ids = list(dict.fromkeys(int(template_id) for template_id in raw_ids))
        except (TypeError, ValueError):
            return None, None, 'Shift selections are invalid.'
    if not name:
        return None, None, 'Enter a group name.'
    if not template_ids:
        return None, None, 'Select at least one shift.'
    duplicate = ShiftStatsGroup.objects.filter(name__iexact=name)
    if instance is not None:
        duplicate = duplicate.exclude(id=instance.id)
    if duplicate.exists():
        return None, None, 'A Stats group with this name already exists.'
    if ShiftTemplate.objects.filter(id__in=template_ids).count() != len(template_ids):
        return None, None, 'One or more selected shifts no longer exist.'
    return name, template_ids, None


@api_view(['GET', 'POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def stats_groups_list_create(request):
    if request.method == 'GET':
        allowed_domain_ids = permitted_domain_ids(request.user, 'view_domain_statistics')
        groups = ShiftStatsGroup.objects.filter(
            shift_templates__domain_id__in=allowed_domain_ids,
        ).prefetch_related('shift_templates').distinct()
        return Response([
            _stats_group_payload(group, allowed_domain_ids=allowed_domain_ids)
            for group in groups
        ])
    name, template_ids, error = _validate_stats_group_data(request.data)
    if error:
        return Response({'detail': error}, status=status.HTTP_400_BAD_REQUEST)
    template_domain_ids = set(ShiftTemplate.objects.filter(
        id__in=template_ids,
    ).values_list('domain_id', flat=True))
    if not template_domain_ids.issubset(
        permitted_domain_ids(request.user, 'manage_build_workspace')
    ):
        return _build_workspace_forbidden_response()
    with transaction.atomic():
        group = ShiftStatsGroup.objects.create(name=name, created_by=request.user)
        group.shift_templates.set(template_ids)
    return Response(_stats_group_payload(group), status=status.HTTP_201_CREATED)


@api_view(['PATCH', 'DELETE'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def stats_group_detail(request, group_id):
    group = get_object_or_404(ShiftStatsGroup.objects.prefetch_related('shift_templates'), id=group_id)
    allowed_domain_ids = permitted_domain_ids(request.user, 'manage_build_workspace')
    existing_domain_ids = set(group.shift_templates.values_list('domain_id', flat=True))
    if not existing_domain_ids or not existing_domain_ids.issubset(allowed_domain_ids):
        return _build_workspace_forbidden_response()
    if request.method == 'DELETE':
        group.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
    name, template_ids, error = _validate_stats_group_data(request.data, group)
    if error:
        return Response({'detail': error}, status=status.HTTP_400_BAD_REQUEST)
    target_domain_ids = set(ShiftTemplate.objects.filter(
        id__in=template_ids,
    ).values_list('domain_id', flat=True))
    if not target_domain_ids.issubset(allowed_domain_ids):
        return _build_workspace_forbidden_response()
    with transaction.atomic():
        group.name = name
        group.save(update_fields=['name', 'updated_at'])
        group.shift_templates.set(template_ids)
    return Response(_stats_group_payload(group))


def _has_published_overlap(domain, start_date, end_date, exclude_id=None):
    published_blocks = ScheduleBlock.objects.filter(
        domain=domain,
        published_at__isnull=False,
    )
    if exclude_id is not None:
        published_blocks = published_blocks.exclude(id=exclude_id)
    return published_blocks.filter(start_date__lte=end_date, end_date__gte=start_date).exists()


def _can_manage_requests(user, domain=None):
    return (
        has_permission(user, 'administer_requests', domain=domain)
        if domain is not None
        else bool(permitted_domain_ids(user, 'administer_requests'))
    )


def _can_submit_own_requests(user, domain=None):
    return (
        has_permission(user, 'submit_own_requests', domain=domain)
        if domain is not None
        else bool(permitted_domain_ids(user, 'submit_own_requests'))
    )


def _request_blocks_for_regular_user(user):
    """Return the relevant published and upcoming block for each accessible Domain."""
    request_domain_ids = permitted_domain_ids(user, 'submit_own_requests')
    block_ids = []
    for domain_id in request_domain_ids:
        domain_blocks = ScheduleBlock.objects.filter(domain_id=domain_id)
        latest_published = (
            domain_blocks.filter(published_at__isnull=False)
            .order_by('-published_at')
            .first()
        )
        upcoming = (
            domain_blocks.filter(
                published_at__isnull=True,
                end_date__gte=timezone.localdate(),
            )
            .order_by('start_date', 'created_at')
            .first()
        )
        if latest_published is not None:
            block_ids.append(latest_published.id)
        if upcoming is not None:
            block_ids.append(upcoming.id)
    return ScheduleBlock.objects.filter(
        Q(id__in=block_ids)
        | Q(
            domain_id__in=permitted_domain_ids(user, 'view_preview'),
            build_status=ScheduleBlock.BuildStatus.PREVIEW,
        )
    )


def _schedule_block_capabilities(user, block):
    can_manage_build = _can_manage_build_workspace(user, block.domain)
    can_administer_requests = _can_manage_requests(user, block.domain)
    can_submit_requests = _can_submit_own_requests(user, block.domain)
    can_view_preview = has_permission(user, 'view_preview', domain=block.domain)
    return {
        'can_manage_build_workspace': can_manage_build,
        'can_administer_requests': can_administer_requests,
        'can_submit_own_requests': can_submit_requests,
        'can_view_preview': can_view_preview,
        'can_open_build_workspace': can_manage_build or (
            block.build_status == ScheduleBlock.BuildStatus.PREVIEW and can_view_preview
        ),
        'can_publish_schedule': can_manage_build and has_permission(
            user, 'publish_schedule', domain=block.domain,
        ),
        'can_unpublish_schedule': can_manage_build and has_permission(
            user, 'unpublish_schedule', domain=block.domain,
        ),
    }


def _serialize_schedule_block_for_user(user, block):
    return {
        **ScheduleBlockSerializer(block).data,
        **_schedule_block_capabilities(user, block),
    }


def _can_access_schedule_block(user, block):
    if _can_manage_build_workspace(user, block.domain) or _can_manage_requests(user, block.domain):
        return True
    if (
        block.build_status == ScheduleBlock.BuildStatus.PREVIEW
        and has_permission(user, 'view_preview', domain=block.domain)
    ):
        return True
    return _request_blocks_for_regular_user(user).filter(id=block.id).exists()


def _request_window_is_open(block):
    now = timezone.now()
    return block.request_open_datetime <= now <= block.request_close_datetime


def _can_manage_build_workspace(user, domain=None):
    return (
        has_permission(user, 'manage_build_workspace', domain=domain)
        if domain is not None
        else bool(permitted_domain_ids(user, 'manage_build_workspace'))
    )


def _editable_request_status(block):
    return block.build_status in {ScheduleBlock.BuildStatus.PRE_BUILD, ScheduleBlock.BuildStatus.BUILD}


def _resolve_self_physician(user):
    try:
        return user.physician
    except Physician.DoesNotExist:
        return None


def _parse_request_date(raw_value):
    if not raw_value or not isinstance(raw_value, str):
        return None

    try:
        return datetime.strptime(raw_value, '%Y-%m-%d').date()
    except ValueError:
        return None


def _domain_has_memberships(domain):
    return bool(domain and DomainMembership.objects.filter(domain=domain).exists())


def _working_request_physicians(block):
    """Return active users allowed to work in this Schedule Block's domain."""
    physicians = Physician.objects.filter(active=True).select_related('user')
    if _domain_has_memberships(block.domain):
        working_user_ids = DomainMembership.objects.filter(
            domain=block.domain,
            active=True,
            clinically_active=True,
        ).exclude(
            role=DomainMembership.Role.VIEW_ONLY,
        ).exclude(
            role_template__system_key='view_only',
        ).values_list('user_id', flat=True)
        physicians = physicians.filter(
            user_id__in=working_user_ids,
        )
    return physicians.distinct().order_by('user__last_name', 'user__first_name')


def _get_request_contract(physician, domain=None):
    """Return the single active contract when Request Builder can resolve one unambiguously."""
    contracts_query = Contract.objects.filter(
        active=True,
        user_assignments__physician=physician,
    )
    if domain is not None:
        domain_contracts = contracts_query.filter(domain=domain)
        if domain_contracts.exists() or _domain_has_memberships(domain):
            contracts_query = domain_contracts

    contracts = list(
        contracts_query
        .prefetch_related('facilities')
        .distinct()[:2]
    )
    return contracts[0] if len(contracts) == 1 else None


def _parse_request_limit(value):
    if value in (None, ''):
        return None

    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None

    return parsed if parsed >= 0 else None


def _get_request_policy(physician, can_manage=False, domain=None):
    all_types = [choice[0] for choice in ScheduleRequest.RequestType.choices]
    contract = _get_request_contract(physician, domain)

    if contract is None:
        return {
            'contract_id': None,
            'contract_name': None,
            'domain_id': domain.id if domain else None,
            'allowed_request_types': all_types if can_manage else [],
            'eligible_facility_ids': None,
            'limits': {
                'HIGH': None,
                'MEDIUM': None,
                'LOW': None,
                'WEEKEND': None,
            },
            'low_unlimited': True,
        }

    settings = contract.request_settings if isinstance(contract.request_settings, dict) else {}
    setting_names = {
        ScheduleRequest.RequestType.DAY_OFF: 'allow_day_off',
        ScheduleRequest.RequestType.SHIFT_OFF: 'allow_shift_off',
        ScheduleRequest.RequestType.DAY_ON: 'allow_day_on',
        ScheduleRequest.RequestType.SHIFT_ON: 'allow_shift_on',
    }
    contract_allowed_types = [
        request_type
        for request_type in all_types
        if settings.get(setting_names[request_type], True) is True
    ]

    return {
        'contract_id': contract.id,
        'contract_name': contract.name,
        'domain_id': contract.domain_id,
        'allowed_request_types': all_types if can_manage else contract_allowed_types,
        'eligible_facility_ids': list(contract.facilities.values_list('id', flat=True)),
        'limits': {
            'HIGH': _parse_request_limit(settings.get('high_request_limit')),
            'MEDIUM': _parse_request_limit(settings.get('medium_request_limit')),
            'LOW': _parse_request_limit(settings.get('low_request_limit')),
            'WEEKEND': _parse_request_limit(settings.get('weekend_request_limit')),
        },
        'low_unlimited': bool(settings.get('low_request_unlimited', False)),
    }


def _get_eligible_shift_templates(eligible_facility_ids=None, domain_id=None):
    templates = _ordered_shift_templates(ShiftTemplate.objects.filter(active=True))
    if domain_id is not None:
        templates = templates.filter(domain_id=domain_id)
    if eligible_facility_ids is not None:
        templates = templates.filter(facility_id__in=eligible_facility_ids)
    return list(templates)


def _get_available_shift_templates_for_date(target_date, eligible_facility_ids=None, domain_id=None):
    day_name = target_date.strftime('%A')
    return [
        template
        for template in _get_eligible_shift_templates(eligible_facility_ids, domain_id)
        if day_name in (template.active_days_of_week or [])
    ]


def _request_counts_as_weekend(schedule_request, eligible_facility_ids=None):
    day_name = schedule_request.date.strftime('%A')

    if schedule_request.request_type == ScheduleRequest.RequestType.DAY_OFF:
        available_templates = _get_available_shift_templates_for_date(
            schedule_request.date,
            eligible_facility_ids,
            schedule_request.schedule_block.domain_id,
        )
        return any(day_name in (template.weekend_days or []) for template in available_templates)

    if schedule_request.request_type == ScheduleRequest.RequestType.SHIFT_OFF:
        return any(
            day_name in (template.weekend_days or [])
            for template in schedule_request.shift_templates.all()
        )

    return False


def _build_request_counters(block, physician, policy, exclude_request_ids=None):
    exclude_request_ids = exclude_request_ids or []
    requests = (
        ScheduleRequest.objects.filter(
            schedule_block=block,
            physician=physician,
            request_scope=ScheduleRequest.RequestScope.USER,
            date__gte=block.start_date,
            date__lte=block.end_date,
        )
        .exclude(id__in=exclude_request_ids)
        .prefetch_related('shift_templates__facility')
    )

    used = {'HIGH': 0, 'MEDIUM': 0, 'LOW': 0, 'WEEKEND': 0}
    for schedule_request in requests:
        if schedule_request.weight in used:
            used[schedule_request.weight] += 1
        if _request_counts_as_weekend(schedule_request, policy['eligible_facility_ids']):
            used['WEEKEND'] += 1

    return {
        key: {
            'used': count,
            'limit': policy['limits'][key],
            'unlimited': key == 'LOW' and policy['low_unlimited'],
        }
        for key, count in used.items()
    }


def _request_counter_increments(request_date, request_type, weight, selected_templates, policy):
    increments = {'HIGH': 0, 'MEDIUM': 0, 'LOW': 0, 'WEEKEND': 0}
    if weight in increments:
        increments[weight] = 1

    day_name = request_date.strftime('%A')
    if request_type == ScheduleRequest.RequestType.DAY_OFF:
        available_templates = _get_available_shift_templates_for_date(
            request_date,
            policy['eligible_facility_ids'],
            policy['domain_id'],
        )
        increments['WEEKEND'] = int(
            any(day_name in (template.weekend_days or []) for template in available_templates)
        )
    elif request_type == ScheduleRequest.RequestType.SHIFT_OFF:
        increments['WEEKEND'] = int(
            any(day_name in (template.weekend_days or []) for template in selected_templates)
        )

    return increments


def _prospective_request_limit_error(
    block,
    physician,
    policy,
    request_date,
    request_type,
    weight,
    selected_templates,
    request_scope,
    exclude_request_ids=None,
):
    if request_scope != ScheduleRequest.RequestScope.USER:
        return None

    counters = _build_request_counters(block, physician, policy, exclude_request_ids)
    increments = _request_counter_increments(
        request_date,
        request_type,
        weight,
        selected_templates,
        policy,
    )

    for key, increment in increments.items():
        if not increment:
            continue
        counter = counters[key]
        if counter['unlimited']:
            continue
        if counter['limit'] is not None and counter['used'] + increment > counter['limit']:
            return {
                'request_limit': (
                    f'{key.title()} request limit of {counter["limit"]} has been reached.'
                )
            }

    return None


def _serialize_physician_choice(physician):
    display_name = physician.display_name or physician.user.get_full_name() or physician.user.username
    return {
        'id': physician.id,
        'name': display_name,
    }


def _validate_request_payload(
    request_type,
    weight,
    shift_template_ids,
    available_template_ids,
    eligible_template_ids,
):
    allowed_types = {choice[0] for choice in ScheduleRequest.RequestType.choices}
    if request_type not in allowed_types:
        return {'request_type': 'Invalid request type.'}

    allowed_weights = {choice[0] for choice in ScheduleRequest.Weight.choices}
    if weight not in allowed_weights:
        return {'weight': 'Weight is required and must be one of LOW, MEDIUM, HIGH, or FIXED.'}

    if request_type in {ScheduleRequest.RequestType.DAY_OFF, ScheduleRequest.RequestType.DAY_ON}:
        if shift_template_ids:
            return {'shift_template_ids': 'Do not select shift templates for Day Off or Day On requests.'}
        return None

    if request_type == ScheduleRequest.RequestType.SHIFT_OFF and not shift_template_ids:
        return {'shift_template_ids': 'Select one or more shift templates for Shift Off requests.'}

    if request_type == ScheduleRequest.RequestType.SHIFT_ON and len(shift_template_ids) != 1:
        return {'shift_template_ids': 'Select exactly one shift template for Shift On requests.'}

    invalid_template_ids = [
        template_id
        for template_id in shift_template_ids
        if template_id not in eligible_template_ids
    ]
    if invalid_template_ids:
        return {'shift_template_ids': 'One or more selected shift templates are not active or eligible for this physician.'}

    if request_type == ScheduleRequest.RequestType.SHIFT_ON:
        if shift_template_ids[0] not in available_template_ids:
            return {'shift_template_ids': 'The selected Shift On template is not available on this date.'}

    if request_type == ScheduleRequest.RequestType.SHIFT_OFF:
        if not set(shift_template_ids).intersection(available_template_ids):
            return {
                'shift_template_ids': (
                    'At least one selected Shift Off template must be available on this date.'
                )
            }

    return None


@api_view(['GET'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_block_requests_list(request, block_id):
    block = get_object_or_404(ScheduleBlock, id=block_id)
    if not (
        _can_manage_requests(request.user, block.domain)
        or _can_submit_own_requests(request.user, block.domain)
    ):
        return Response(
            {'detail': 'Schedule request access is required for this Domain.'},
            status=status.HTTP_403_FORBIDDEN,
        )
    requests = (
        ScheduleRequest.objects.filter(
            schedule_block=block,
            date__gte=block.start_date,
            date__lte=block.end_date,
        )
        .select_related('physician__user')
        .prefetch_related('shift_templates__facility')
    )

    if not _can_manage_requests(request.user, block.domain):
        physician = _resolve_self_physician(request.user)
        if physician is None:
            requests = ScheduleRequest.objects.none()
        else:
            requests = requests.filter(
                physician=physician,
                request_scope=ScheduleRequest.RequestScope.USER,
            )

    return Response(ScheduleRequestSerializer(requests, many=True).data)


@api_view(['GET'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_block_requests_context(request, block_id):
    block = get_object_or_404(ScheduleBlock, id=block_id)
    can_manage = _can_manage_requests(request.user, block.domain)
    can_submit_own = _can_submit_own_requests(request.user, block.domain)
    if not can_manage and not can_submit_own:
        return Response(
            {'detail': 'Schedule request access is required for this Domain.'},
            status=status.HTTP_403_FORBIDDEN,
        )

    if can_manage:
        physicians = list(_working_request_physicians(block))
    else:
        self_physician = _resolve_self_physician(request.user)
        working_physician_ids = set(
            _working_request_physicians(block).filter(id=self_physician.id).values_list('id', flat=True)
        ) if self_physician else set()
        physicians = [self_physician] if self_physician and self_physician.id in working_physician_ids else []

    selected_physician_id_param = request.query_params.get('physician_id')
    selected_physician_id = physicians[0].id if physicians else None

    if selected_physician_id_param:
        try:
            requested_physician_id = int(selected_physician_id_param)
        except (TypeError, ValueError):
            return Response({'physician_id': 'physician_id must be a valid integer.'}, status=status.HTTP_400_BAD_REQUEST)

        if any(physician.id == requested_physician_id for physician in physicians):
            selected_physician_id = requested_physician_id
        else:
            return Response({'detail': 'You do not have permission to view requests for this physician.'}, status=status.HTTP_403_FORBIDDEN)

    request_items = (
        ScheduleRequest.objects.filter(
            schedule_block=block,
            physician_id=selected_physician_id,
            date__gte=block.start_date,
            date__lte=block.end_date,
        )
        .select_related('physician__user')
        .prefetch_related('shift_templates__facility')
        if selected_physician_id
        else ScheduleRequest.objects.none()
    )
    if not can_manage:
        request_items = request_items.filter(request_scope=ScheduleRequest.RequestScope.USER)

    selected_physician = next(
        (physician for physician in physicians if physician.id == selected_physician_id),
        None,
    )
    policy = _get_request_policy(selected_physician, can_manage, block.domain) if selected_physician else None

    visible_requests = (
        ScheduleRequest.objects.filter(
            schedule_block=block,
            date__gte=block.start_date,
            date__lte=block.end_date,
        )
        .select_related('physician__user')
        .prefetch_related('shift_templates__facility')
        if can_manage
        else request_items
    )

    templates = _ordered_shift_templates(ShiftTemplate.objects.filter(active=True, domain=block.domain))
    if policy and policy['eligible_facility_ids'] is not None:
        templates = templates.filter(facility_id__in=policy['eligible_facility_ids'])

    serialized_templates = ShiftTemplateSerializer(templates, many=True).data
    counters = (
        _build_request_counters(block, selected_physician, policy)
        if selected_physician and policy
        else {
            key: {'used': 0, 'limit': None, 'unlimited': key == 'LOW'}
            for key in ['HIGH', 'MEDIUM', 'LOW', 'WEEKEND']
        }
    )

    return Response(
        {
            'schedule_block': ScheduleBlockSerializer(block).data,
            'can_manage_requests': can_manage,
            'can_submit_own_requests': can_submit_own,
            'is_scheduler_or_admin': can_manage,
            'selected_physician_id': selected_physician_id,
            'physicians': [_serialize_physician_choice(physician) for physician in physicians],
            'requests': ScheduleRequestSerializer(request_items, many=True).data,
            'visible_requests': ScheduleRequestSerializer(visible_requests, many=True).data,
            'shift_templates': serialized_templates,
            'request_policy': policy,
            'request_counters': counters,
        }
    )


@api_view(['POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_block_request_upsert(request, block_id):
    block = get_object_or_404(ScheduleBlock, id=block_id)
    if not _editable_request_status(block):
        return Response(
            {'detail': 'Requests can only be entered for PRE_BUILD or BUILD Schedule Blocks.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    can_manage = _can_manage_requests(request.user, block.domain)
    self_physician = _resolve_self_physician(request.user)
    if not can_manage and not has_permission(request.user, 'submit_own_requests', domain=block.domain):
        return Response(
            {'detail': 'Request submission permission is required for this Domain.'},
            status=status.HTTP_403_FORBIDDEN,
        )
    if not can_manage and not _request_window_is_open(block):
        return Response(
            {'detail': 'The request window for this Schedule Block is not open.'},
            status=status.HTTP_403_FORBIDDEN,
        )

    physician_id = request.data.get('physician_id')
    try:
        physician_id = int(physician_id)
    except (TypeError, ValueError):
        return Response({'physician_id': 'physician_id is required and must be a valid integer.'}, status=status.HTTP_400_BAD_REQUEST)

    if can_manage:
        physician = _working_request_physicians(block).filter(id=physician_id).first()
        if physician is None:
            return Response(
                {'physician_id': 'The selected user does not have working access to this Schedule Block domain.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
    else:
        if not self_physician:
            return Response({'detail': 'Authenticated user is not linked to a physician profile.'}, status=status.HTTP_403_FORBIDDEN)
        if physician_id != self_physician.id:
            return Response({'detail': 'You do not have permission to modify requests for this physician.'}, status=status.HTTP_403_FORBIDDEN)
        physician = self_physician
        if not _working_request_physicians(block).filter(id=physician.id).exists():
            return Response(
                {'detail': 'You do not have working access to this Schedule Block domain.'},
                status=status.HTTP_403_FORBIDDEN,
            )
    policy = _get_request_policy(physician, can_manage, block.domain)

    parsed_date = _parse_request_date(request.data.get('date'))
    if not parsed_date:
        return Response({'date': 'date is required and must be in YYYY-MM-DD format.'}, status=status.HTTP_400_BAD_REQUEST)

    if parsed_date < block.start_date or parsed_date > block.end_date:
        return Response({'date': 'Date must be within the selected Schedule Block range.'}, status=status.HTTP_400_BAD_REQUEST)

    request_scope = str(request.data.get('request_scope') or ScheduleRequest.RequestScope.USER).upper()
    allowed_scopes = {choice[0] for choice in ScheduleRequest.RequestScope.choices}
    if request_scope not in allowed_scopes:
        return Response({'request_scope': 'Invalid request scope.'}, status=status.HTTP_400_BAD_REQUEST)

    if request_scope == ScheduleRequest.RequestScope.ADMIN and not can_manage:
        return Response({'detail': 'Only admin/scheduler users can create admin requests.'}, status=status.HTTP_403_FORBIDDEN)

    request_type = str(request.data.get('request_type') or '').upper()
    if request_type == 'NONE':
        deleted, _ = ScheduleRequest.objects.filter(
            schedule_block=block,
            physician=physician,
            date=parsed_date,
            request_scope=request_scope,
        ).delete()
        return Response({'deleted': bool(deleted)})

    if not can_manage and request_type not in policy['allowed_request_types']:
        return Response(
            {'request_type': 'This request type is not allowed by your contract.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    weight = str(request.data.get('weight') or '').upper()

    raw_shift_template_ids = request.data.get('shift_template_ids') or []
    if not isinstance(raw_shift_template_ids, list):
        return Response({'shift_template_ids': 'shift_template_ids must be an array of ids.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        shift_template_ids = sorted({int(template_id) for template_id in raw_shift_template_ids})
    except (TypeError, ValueError):
        return Response({'shift_template_ids': 'shift_template_ids must contain only integer ids.'}, status=status.HTTP_400_BAD_REQUEST)

    eligible_templates = _get_eligible_shift_templates(
        policy['eligible_facility_ids'],
        block.domain_id,
    )
    eligible_template_ids = {template.id for template in eligible_templates}
    day_name = parsed_date.strftime('%A')
    available_templates = [
        template
        for template in eligible_templates
        if day_name in (template.active_days_of_week or [])
    ]
    available_template_ids = {template.id for template in available_templates}

    payload_error = _validate_request_payload(
        request_type,
        weight,
        shift_template_ids,
        available_template_ids,
        eligible_template_ids,
    )
    if payload_error:
        return Response(payload_error, status=status.HTTP_400_BAD_REQUEST)

    selected_templates = [
        template for template in eligible_templates if template.id in shift_template_ids
    ]
    applicable_selected_templates = [
        template for template in available_templates if template.id in shift_template_ids
    ]
    existing_request = ScheduleRequest.objects.filter(
        schedule_block=block,
        physician=physician,
        date=parsed_date,
        request_scope=request_scope,
    ).first()
    limit_error = _prospective_request_limit_error(
        block,
        physician,
        policy,
        parsed_date,
        request_type,
        weight,
        applicable_selected_templates,
        request_scope,
        [existing_request.id] if existing_request else None,
    )
    if limit_error:
        return Response(limit_error, status=status.HTTP_400_BAD_REQUEST)

    schedule_request, _ = ScheduleRequest.objects.get_or_create(
        schedule_block=block,
        physician=physician,
        date=parsed_date,
        request_scope=request_scope,
        defaults={
            'request_type': request_type,
            'weight': weight,
            'created_by': request.user,
        },
    )

    schedule_request.request_type = request_type
    schedule_request.weight = weight
    schedule_request.created_by = request.user
    schedule_request.save()

    if shift_template_ids:
        schedule_request.shift_templates.set(ShiftTemplate.objects.filter(id__in=shift_template_ids))
    else:
        schedule_request.shift_templates.clear()

    return Response(ScheduleRequestSerializer(schedule_request).data)


@api_view(['GET', 'DELETE'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_block_request_detail(request, block_id, request_id):
    block = get_object_or_404(ScheduleBlock, id=block_id)
    schedule_request = get_object_or_404(
        ScheduleRequest.objects.select_related('physician__user').prefetch_related('shift_templates__facility'),
        id=request_id,
        schedule_block=block,
        date__gte=block.start_date,
        date__lte=block.end_date,
    )

    can_manage = _can_manage_requests(request.user, block.domain)
    can_submit_own = _can_submit_own_requests(request.user, block.domain)
    if not can_manage and not can_submit_own:
        return Response(
            {'detail': 'Schedule request access is required for this Domain.'},
            status=status.HTTP_403_FORBIDDEN,
        )
    self_physician = _resolve_self_physician(request.user)
    if not can_manage and (
        self_physician is None
        or schedule_request.physician_id != self_physician.id
        or schedule_request.request_scope != ScheduleRequest.RequestScope.USER
    ):
        return Response({'detail': 'You do not have permission to access this request.'}, status=status.HTTP_403_FORBIDDEN)

    if request.method == 'GET':
        return Response(ScheduleRequestSerializer(schedule_request).data)

    if not can_manage and not can_submit_own:
        return Response(
            {'detail': 'Request submission permission is required for this Domain.'},
            status=status.HTTP_403_FORBIDDEN,
        )

    if not can_manage and not _request_window_is_open(block):
        return Response(
            {'detail': 'The request window for this Schedule Block is not open.'},
            status=status.HTTP_403_FORBIDDEN,
        )

    if not _editable_request_status(block):
        return Response(
            {'detail': 'Requests can only be removed from PRE_BUILD or BUILD Schedule Blocks.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    schedule_request.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_block_clear_requests(request, block_id):
    block = get_object_or_404(ScheduleBlock, id=block_id)
    if not _can_manage_requests(request.user, block.domain):
        return Response(
            {'detail': 'Only admin/scheduler users can clear Schedule Block requests.'},
            status=status.HTTP_403_FORBIDDEN,
        )

    if not _editable_request_status(block):
        return Response(
            {'detail': 'Requests can only be cleared from PRE_BUILD or BUILD Schedule Blocks.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    request_scope = str(request.data.get('request_scope') or '').upper()
    if request_scope not in {
        ScheduleRequest.RequestScope.USER,
        ScheduleRequest.RequestScope.ADMIN,
    }:
        return Response(
            {'request_scope': 'request_scope must be USER or ADMIN.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    deleted_count, _ = ScheduleRequest.objects.filter(
        schedule_block=block,
        request_scope=request_scope,
    ).delete()
    return Response({'deleted_count': deleted_count, 'request_scope': request_scope})


@api_view(['POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_block_bulk_requests(request, block_id):
    block = get_object_or_404(ScheduleBlock, id=block_id)
    if not _editable_request_status(block):
        return Response(
            {'detail': 'Bulk requests can only be entered for PRE_BUILD or BUILD Schedule Blocks.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if not _can_manage_requests(request.user, block.domain):
        return Response({'detail': 'Only admin/scheduler users can create bulk requests.'}, status=status.HTTP_403_FORBIDDEN)

    request_scope = str(request.data.get('request_scope') or ScheduleRequest.RequestScope.USER).upper()
    allowed_scopes = {choice[0] for choice in ScheduleRequest.RequestScope.choices}
    if request_scope not in allowed_scopes:
        return Response({'request_scope': 'Invalid request scope.'}, status=status.HTTP_400_BAD_REQUEST)

    request_type = str(request.data.get('request_type') or '').upper()
    if request_type == 'NONE':
        return Response({'request_type': 'Bulk action does not support NONE.'}, status=status.HTTP_400_BAD_REQUEST)

    weight = str(request.data.get('weight') or '').upper()

    physician_ids = request.data.get('physician_ids') or []
    if not isinstance(physician_ids, list) or not physician_ids:
        return Response({'physician_ids': 'Select one or more physicians.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        physician_ids = sorted({int(physician_id) for physician_id in physician_ids})
    except (TypeError, ValueError):
        return Response({'physician_ids': 'physician_ids must contain only integer ids.'}, status=status.HTTP_400_BAD_REQUEST)

    physicians = list(_working_request_physicians(block).filter(id__in=physician_ids))
    if len(physicians) != len(physician_ids):
        return Response(
            {'physician_ids': 'One or more selected users do not have working access to this Schedule Block domain.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    raw_dates = request.data.get('dates') or []
    if not isinstance(raw_dates, list) or not raw_dates:
        return Response({'dates': 'Select one or more dates.'}, status=status.HTTP_400_BAD_REQUEST)

    parsed_dates = []
    for raw_date in raw_dates:
        parsed_date = _parse_request_date(raw_date)
        if not parsed_date:
            return Response({'dates': 'All dates must be in YYYY-MM-DD format.'}, status=status.HTTP_400_BAD_REQUEST)
        if parsed_date < block.start_date or parsed_date > block.end_date:
            return Response({'dates': 'All dates must be within the selected Schedule Block range.'}, status=status.HTTP_400_BAD_REQUEST)
        parsed_dates.append(parsed_date)

    parsed_dates = sorted(set(parsed_dates))

    raw_shift_template_ids = request.data.get('shift_template_ids') or []
    if not isinstance(raw_shift_template_ids, list):
        return Response({'shift_template_ids': 'shift_template_ids must be an array of ids.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        shift_template_ids = sorted({int(template_id) for template_id in raw_shift_template_ids})
    except (TypeError, ValueError):
        return Response({'shift_template_ids': 'shift_template_ids must contain only integer ids.'}, status=status.HTTP_400_BAD_REQUEST)

    plans = {}
    for physician in physicians:
        policy = _get_request_policy(physician, can_manage=True, domain=block.domain)
        eligible_templates = _get_eligible_shift_templates(
            policy['eligible_facility_ids'],
            block.domain_id,
        )
        eligible_template_ids = {template.id for template in eligible_templates}
        selected_templates = [
            template for template in eligible_templates if template.id in shift_template_ids
        ]
        existing_target_ids = list(
            ScheduleRequest.objects.filter(
                schedule_block=block,
                physician=physician,
                date__in=parsed_dates,
                request_scope=request_scope,
            ).values_list('id', flat=True)
        )
        projected_counters = _build_request_counters(
            block,
            physician,
            policy,
            existing_target_ids if request_scope == ScheduleRequest.RequestScope.USER else None,
        )

        for parsed_date in parsed_dates:
            day_name = parsed_date.strftime('%A')
            available_templates = [
                template
                for template in eligible_templates
                if day_name in (template.active_days_of_week or [])
            ]
            available_template_ids = {template.id for template in available_templates}
            payload_error = _validate_request_payload(
                request_type,
                weight,
                shift_template_ids,
                available_template_ids,
                eligible_template_ids,
            )
            if payload_error:
                payload_error['date'] = parsed_date.isoformat()
                payload_error['physician_id'] = physician.id
                return Response(payload_error, status=status.HTTP_400_BAD_REQUEST)

            applicable_selected_templates = [
                template for template in available_templates if template.id in shift_template_ids
            ]
            plans[(physician.id, parsed_date)] = selected_templates

            if request_scope != ScheduleRequest.RequestScope.USER:
                continue

            increments = _request_counter_increments(
                parsed_date,
                request_type,
                weight,
                applicable_selected_templates,
                policy,
            )
            for key, increment in increments.items():
                if not increment:
                    continue
                counter = projected_counters[key]
                if not counter['unlimited'] and counter['limit'] is not None:
                    if counter['used'] + increment > counter['limit']:
                        return Response(
                            {
                                'request_limit': (
                                    f'{key.title()} request limit of {counter["limit"]} '
                                    f'has been reached for {_serialize_physician_choice(physician)["name"]}.'
                                ),
                                'physician_id': physician.id,
                                'date': parsed_date.isoformat(),
                            },
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                counter['used'] += increment

    saved_count = 0
    with transaction.atomic():
        for physician in physicians:
            for parsed_date in parsed_dates:
                schedule_request, _ = ScheduleRequest.objects.get_or_create(
                    schedule_block=block,
                    physician=physician,
                    date=parsed_date,
                    request_scope=request_scope,
                    defaults={
                        'request_type': request_type,
                        'weight': weight,
                        'created_by': request.user,
                    },
                )
                schedule_request.request_type = request_type
                schedule_request.weight = weight
                schedule_request.created_by = request.user
                schedule_request.save()
                selected_templates = plans[(physician.id, parsed_date)]
                if selected_templates:
                    schedule_request.shift_templates.set(selected_templates)
                else:
                    schedule_request.shift_templates.clear()
                saved_count += 1

    return Response({'saved_count': saved_count, 'request_scope': request_scope})


def _build_workspace_forbidden_response():
    return Response(
        {'detail': 'Only admin/scheduler users can manage the Schedule Build Workspace.'},
        status=status.HTTP_403_FORBIDDEN,
    )


def _schedule_version_queryset(block):
    return (
        ScheduleVersion.objects.filter(schedule_block=block, domain=block.domain)
        .select_related('domain', 'published_optimizer_run')
    )


def _active_optimizer_run(version):
    return get_active_optimizer_run(version)


def _mark_schedule_score_stale(version, viewed_run=None):
    ScheduleVersion.objects.filter(id=version.id).update(score_is_stale=True)
    if viewed_run is not None:
        OptimizerRun.objects.filter(id=viewed_run.id).update(score_is_stale=True)


def _cleanup_stale_optimizer_runs(version):
    # A control row means the background worker owns this run. Do not attempt
    # to update the same OptimizerRun row while optimize_schedule_version holds
    # its transaction lock: that made the build workspace wait indefinitely
    # when a search overran. The enqueue path separately removes controls older
    # than 15 minutes before deciding whether a new run may start.
    controlled_run_ids = OptimizerControl.objects.filter(
        schedule_version=version,
        optimizer_run_id__isnull=False,
    ).values_list('optimizer_run_id', flat=True)
    stale_before = timezone.now() - timedelta(minutes=STALE_OPTIMIZER_RUN_MINUTES)
    stale_runs = OptimizerRun.objects.filter(
        schedule_version=version,
        status=OptimizerRun.Status.RUNNING,
        created_at__lt=stale_before,
    ).exclude(id__in=controlled_run_ids)
    stale_count = stale_runs.count()
    if stale_count:
        stale_runs.update(
            status=OptimizerRun.Status.FAILED,
            is_active=False,
            notes='Optimizer marked failed after exceeding stale running threshold.',
        )
    return stale_count


def _optimizer_concurrency_limit():
    return max(
        1,
        int(getattr(settings, 'OPTIMIZER_MAX_CONCURRENT_RUNS_PER_VERSION', 1)),
    )


def _running_optimizer_runs(version):
    _cleanup_stale_optimizer_runs(version)
    return version.optimizer_runs.filter(
        status=OptimizerRun.Status.RUNNING,
    ).order_by('run_number')


def _optimizer_capacity(version):
    running_runs = _running_optimizer_runs(version)
    running_count = running_runs.count()
    concurrency_limit = _optimizer_concurrency_limit()
    protected_source_run_ids = list(
        OptimizerControl.objects.filter(
            schedule_version=version,
            optimizer_run__status=OptimizerRun.Status.RUNNING,
            source_run_id__isnull=False,
        ).values_list('source_run_id', flat=True).distinct()
    )
    return {
        'limit': concurrency_limit,
        'running_count': running_count,
        'available_slots': max(0, concurrency_limit - running_count),
        'running_run_ids': list(running_runs.values_list('id', flat=True)),
        'protected_source_run_ids': protected_source_run_ids,
    }


def _blocking_optimizer_run(version):
    _cleanup_stale_optimizer_runs(version)
    return version.optimizer_runs.filter(status=OptimizerRun.Status.RUNNING).order_by('-run_number').first()


def _default_optimizer_run(version):
    _cleanup_stale_optimizer_runs(version)
    active_run = _active_optimizer_run(version)
    if active_run is not None:
        return active_run
    return version.optimizer_runs.filter(status=OptimizerRun.Status.COMPLETED).order_by('-run_number').first()


def _get_optimizer_run_for_version(version, run_id):
    return get_viewed_optimizer_run(version, run_id)


def _requested_editable_run(request, version):
    requested_id = request.data.get('optimizer_run_id') or request.query_params.get('optimizer_run_id')
    context = resolve_build_workspace_run_context(version, requested_id)
    if requested_id not in (None, '') and (
        context.viewed_run is None or str(context.viewed_run.id) != str(requested_id)
    ):
        return None, Response({'detail': 'The viewed optimizer run was not found.'}, status=status.HTTP_400_BAD_REQUEST)
    if context.viewed_run is not None and not context.viewed_run_is_editable:
        return None, Response({'detail': 'Manual edits apply only to the viewed active run.'}, status=status.HTTP_409_CONFLICT)
    return context.viewed_run, None


def _set_active_run_locked_open(instance, is_locked_open):
    active_run = _active_optimizer_run(instance.schedule_version)
    if active_run is None:
        return
    locked_ids = set(active_run.locked_open_shift_instance_ids or [])
    if is_locked_open:
        locked_ids.add(instance.id)
    else:
        locked_ids.discard(instance.id)
    active_run.locked_open_shift_instance_ids = sorted(locked_ids)
    active_run.save(update_fields=['locked_open_shift_instance_ids'])


def _mark_contract_domain_scores_stale(contract):
    editable_versions = ScheduleVersion.objects.filter(
        domain=contract.domain,
        schedule_block__published_at__isnull=True,
    )
    editable_versions.update(score_is_stale=True)
    OptimizerRun.objects.filter(schedule_version__in=editable_versions).update(score_is_stale=True)


def _shift_instance_queryset(version, optimizer_run=None):
    visible_assignments = (
        ScheduleShiftAssignment.objects
        .filter(visible_assignment_filter(optimizer_run))
        .select_related('physician__user')
        .order_by('physician__user__last_name', 'physician__user__first_name', 'id')
    )
    return (
        ScheduleShiftInstance.objects.filter(schedule_version=version)
        .filter(
            date__gte=version.schedule_block.start_date,
            date__lte=version.schedule_block.end_date,
        )
        .select_related('facility', 'shift_template__facility')
        .prefetch_related(Prefetch(
            'assignments', queryset=visible_assignments,
            to_attr='visible_assignments_cached',
        ))
    )


def _shift_generation_required(block, version):
    if version is None:
        return True
    templates = list(
        ShiftTemplate.objects.filter(active=True, facility__active=True, domain=version.domain)
        .select_related('facility')
        .order_by(*SHIFT_TEMPLATE_DISPLAY_ORDER)
    )
    current_fingerprint = _shift_template_fingerprint(block, templates)
    if version.shift_template_fingerprint != current_fingerprint:
        return True
    return not version.shift_instances.filter(
        date__gte=block.start_date,
        date__lte=block.end_date,
    ).exists()


def _facility_timezone(facility):
    try:
        return ZoneInfo(facility.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return datetime_timezone.utc


@api_view(['GET'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_block_build_context(request, block_id):
    block = get_object_or_404(
        ScheduleBlock.objects.select_related('domain__region', 'preview_optimizer_run'),
        id=block_id,
    )
    can_manage = _can_manage_build_workspace(request.user, block.domain)
    can_view_preview = has_permission(request.user, 'view_preview', domain=block.domain)
    if not can_manage and not (
        block.build_status == ScheduleBlock.BuildStatus.PREVIEW and can_view_preview
    ):
        return _build_workspace_forbidden_response()
    versions = _schedule_version_queryset(block)
    selected_version = None

    version_id = request.query_params.get('version_id')
    if version_id:
        try:
            version_id = int(version_id)
        except (TypeError, ValueError):
            return Response(
                {'version_id': 'version_id must be a valid integer.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        selected_version = get_object_or_404(versions, id=version_id)
    else:
        selected_version = versions.filter(status=ScheduleVersion.Status.BUILD).first() or versions.first()

    optimizer_run_id = request.query_params.get('optimizer_run_id')
    if (
        block.build_status == ScheduleBlock.BuildStatus.PREVIEW
        and block.preview_optimizer_run_id
        and (not optimizer_run_id or not can_manage)
    ):
        optimizer_run_id = str(block.preview_optimizer_run_id)
    if optimizer_run_id and not version_id:
        requested_optimizer_run = OptimizerRun.objects.filter(
            id=optimizer_run_id,
            schedule_version__schedule_block=block,
            schedule_version__domain=block.domain,
        ).select_related('schedule_version').first()
        if requested_optimizer_run is not None:
            selected_version = requested_optimizer_run.schedule_version

    if selected_version:
        _cleanup_stale_optimizer_runs(selected_version)
    run_context = resolve_build_workspace_run_context(selected_version, optimizer_run_id) if selected_version else None
    selected_optimizer_run = run_context.viewed_run if run_context else None
    optimizer_summary = None
    if selected_optimizer_run:
        optimizer_summary = selected_optimizer_run.optimizer_summary or None
    elif selected_version:
        optimizer_summary = selected_version.optimizer_summary or None
    shift_instances = (
        ScheduleShiftInstanceSerializer(
            _shift_instance_queryset(selected_version, selected_optimizer_run),
            many=True,
            context={'optimizer_run_id': selected_optimizer_run.id if selected_optimizer_run else None, 'viewed_run': selected_optimizer_run},
        ).data
        if selected_version
        else []
    )
    workload_feasibility = None
    if selected_version and block.published_at is None:
        feasibility_report = build_workload_feasibility(
            selected_version, selected_optimizer_run,
            include_individual_diagnostics=False,
        )
        workload_feasibility = {
            **feasibility_report['aggregate_feasibility'],
            'total_generated_shift_instances': feasibility_report['schedule_block'][
                'total_generated_shift_instances'
            ],
        }
        workload_feasibility['night_feasibility'] = feasibility_report['night_feasibility']
        workload_feasibility['request_off_feasibility'] = feasibility_report['request_off_feasibility']
        workload_feasibility['weekend_feasibility'] = feasibility_report['weekend_feasibility']

    return Response(
        {
            'schedule_block': ScheduleBlockSerializer(block).data,
            'can_manage_build_workspace': can_manage,
            'can_view_preview': can_view_preview,
            'can_publish_schedule': can_manage and has_permission(
                request.user, 'publish_schedule', domain=block.domain,
            ),
            'atlas_v2_enabled': bool(
                getattr(settings, 'ATLAS_V2_ENABLED', False)
            ),
            # Compatibility response field for older open browser sessions.
            'atlas_v2_test_enabled': bool(
                getattr(settings, 'ATLAS_V2_ENABLED', False)
            ),
            'domains': [
                {
                    'id': block.domain_id,
                    'name': block.domain.name,
                    'region': block.domain.region_id,
                    'region_name': block.domain.region.name,
                }
            ],
            'versions': ScheduleVersionWorkspaceSerializer(versions, many=True).data,
            'selected_version': (
                ScheduleVersionWorkspaceSerializer(selected_version).data
                if selected_version
                else None
            ),
            'shift_generation_required': _shift_generation_required(
                block, selected_version,
            ),
            'optimizer_summary': (
                optimizer_summary
            ),
            'optimizer_runs': (
                OptimizerRunHistorySerializer(
                    selected_version.optimizer_runs
                    .select_related('copied_from_run', 'control', 'schedule_version')
                    .annotate(runtime_seconds_value=Cast('optimizer_summary__runtime_seconds', FloatField()))
                    .defer('optimizer_summary', 'optimizer_debug', 'score_breakdown')
                    .order_by('-run_number'),
                    many=True,
                ).data
                if selected_version
                else []
            ),
            'selected_optimizer_run': (
                OptimizerRunHistorySerializer(selected_optimizer_run).data
                if selected_optimizer_run
                else None
            ),
            'run_state': serialize_run_state(run_context) if run_context else {
                'viewed_run_id': None, 'active_run_id': None,
                'viewed_run_is_editable': False, 'viewed_run_can_activate': False,
                'viewed_run_can_copy': False, 'viewed_run_can_be_optimizer_source': False,
            },
            'optimizer_capacity': (
                _optimizer_capacity(selected_version)
                if selected_version
                else {
                    'limit': _optimizer_concurrency_limit(),
                    'running_count': 0,
                    'available_slots': _optimizer_concurrency_limit(),
                    'running_run_ids': [],
                    'protected_source_run_ids': [],
                }
            ),
            'shift_instances': shift_instances,
            'workload_feasibility': workload_feasibility,
        }
    )


@api_view(['GET'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_block_schedule_versions(request, block_id):
    block = get_object_or_404(ScheduleBlock.objects.select_related('domain'), id=block_id)
    if not _can_manage_build_workspace(request.user, block.domain):
        return _build_workspace_forbidden_response()
    return Response(ScheduleVersionSerializer(_schedule_version_queryset(block), many=True).data)


@api_view(['POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_version_workload_hour_adjustment(request, version_id):
    version = get_object_or_404(
        ScheduleVersion.objects.select_related('schedule_block', 'domain'), id=version_id,
    )
    if not _can_manage_build_workspace(request.user, version.domain):
        return _build_workspace_forbidden_response()
    if version.status != ScheduleVersion.Status.BUILD:
        return Response({'detail': 'Only a Build schedule version can be adjusted.'}, status=status.HTTP_409_CONFLICT)
    if request.data.get('reset') is True:
        version.workload_hour_overrides = {}
        version.score_is_stale = True
        version.save(update_fields=['workload_hour_overrides', 'score_is_stale', 'updated_at'])
        version.optimizer_runs.update(score_is_stale=True)
        return Response({'detail': 'Schedule Block workload adjustments were reset.'})

    try:
        hours_per_fte = Decimal(str(request.data.get('hours_per_fte')))
    except (InvalidOperation, TypeError):
        return Response({'hours_per_fte': 'Enter a valid number.'}, status=status.HTTP_400_BAD_REQUEST)
    if hours_per_fte <= 0 or hours_per_fte > Decimal('1000'):
        return Response({'hours_per_fte': 'Enter a value greater than 0 and no more than 1000.'}, status=status.HTTP_400_BAD_REQUEST)
    if hours_per_fte != hours_per_fte.to_integral_value():
        return Response({'hours_per_fte': 'Enter a whole number of hours.'}, status=status.HTTP_400_BAD_REQUEST)

    report = build_workload_feasibility(version)
    preview = report['aggregate_feasibility'].get('fte_adjustment_preview')
    if not preview or not preview.get('can_preview'):
        return Response({'detail': 'There is no applicable workload discrepancy to adjust.'}, status=status.HTTP_409_CONFLICT)
    direction = preview['direction']
    overrides = dict(version.workload_hour_overrides or {})
    rows = {row['physician_id']: row for row in report['physicians']}
    for proposal in preview['proposals']:
        row = rows[proposal['physician_id']]
        adjustment = hours_per_fte * Decimal(str(row['fte']))
        minimum = Decimal(str(row['effective_min_hours']))
        maximum = Decimal(str(row['effective_max_hours']))
        if direction == 'increase_maximum':
            maximum += adjustment
        else:
            minimum = max(Decimal('0'), minimum - adjustment)
        overrides[str(row['physician_id'])] = {
            'minimum_hours': str(minimum),
            'maximum_hours': str(maximum),
        }
    version.workload_hour_overrides = overrides
    version.score_is_stale = True
    version.save(update_fields=['workload_hour_overrides', 'score_is_stale', 'updated_at'])
    version.optimizer_runs.update(score_is_stale=True)
    return Response({
        'detail': 'Schedule Block workload limits were adjusted. Permanent Contracts were not changed.',
        'direction': direction,
        'hours_per_fte': float(hours_per_fte),
    })


@api_view(['GET'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_version_shift_instances(request, block_id, version_id):
    block = get_object_or_404(ScheduleBlock.objects.select_related('domain'), id=block_id)
    version = get_object_or_404(
        ScheduleVersion.objects.select_related('domain'),
        id=version_id,
        schedule_block=block,
    )
    if not _can_manage_build_workspace(request.user, version.domain):
        return _build_workspace_forbidden_response()
    optimizer_run = _get_optimizer_run_for_version(version, request.query_params.get('optimizer_run_id'))
    return Response(
        ScheduleShiftInstanceSerializer(
            _shift_instance_queryset(version, optimizer_run),
            many=True,
            context={'optimizer_run_id': optimizer_run.id if optimizer_run else None},
        ).data
    )


def _parse_optimizer_seed(request):
    if 'seed' not in request.data or request.data.get('seed') in (None, ''):
        return None, None
    try:
        return int(request.data.get('seed')), None
    except (TypeError, ValueError):
        return None, {'seed': 'seed must be an integer.'}


def _parse_optimizer_max_runtime_seconds(request):
    if 'max_runtime_minutes' not in request.data:
        return None, None
    value = request.data.get('max_runtime_minutes')
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return None, {'max_runtime_minutes': 'Maximum runtime must be a whole number of minutes.'}
    if str(value).strip() != str(minutes):
        return None, {'max_runtime_minutes': 'Maximum runtime must be a whole number of minutes.'}
    if not 1 <= minutes <= 240:
        return None, {'max_runtime_minutes': 'Maximum runtime must be between 1 and 240 minutes.'}
    return minutes * 60, None


def _parse_optimizer_focus(request):
    value = request.data.get(
        'optimization_focus', OptimizerRun.OptimizationFocus.STANDARD,
    )
    if value not in OptimizerRun.OptimizationFocus.values:
        return None, {
            'optimization_focus': 'Use STANDARD or DISTRIBUTION.'
        }
    return value, None


def _parse_optimizer_engine(request):
    value = request.data.get('optimizer_engine', 'V1')
    if value == 'V2_TEST':
        value = 'V2'
    if value not in ('V1', 'V2'):
        return None, {'optimizer_engine': 'Use V1 or V2.'}
    return value, None


def _optimizer_start_options(request, version):
    start_mode = request.data.get('start_mode', OptimizerRun.StartMode.FRESH_FILL)
    if start_mode not in OptimizerRun.StartMode.values:
        return None, None, {'start_mode': 'Use CURRENT_SCHEDULE or FRESH_FILL.'}
    if start_mode == OptimizerRun.StartMode.CURRENT_SCHEDULE:
        run_id = (
            request.data.get('source_run_id')
            or request.data.get('currently_viewed_run_id')
            or request.data.get('optimizer_run_id')
        )
        if run_id in (None, ''):
            return None, None, {
                'source_run_id': 'Select a completed run in this schedule version.'
            }
    else:
        # Fresh Fill does not inherit optimizer assignments from a historical
        # result. The viewed run is accepted only so explicitly locked manual
        # assignments and locked-open shifts remain fixed.
        run_id = request.data.get('currently_viewed_run_id') or request.data.get('optimizer_run_id')
    source_run = None
    if run_id not in (None, ''):
        try:
            source_run = OptimizerRun.objects.get(
                id=int(run_id),
                schedule_version=version,
                status=OptimizerRun.Status.COMPLETED,
            )
        except (TypeError, ValueError, OptimizerRun.DoesNotExist):
            error_field = (
                'source_run_id'
                if start_mode == OptimizerRun.StartMode.CURRENT_SCHEDULE
                else 'currently_viewed_run_id'
            )
            return None, None, {error_field: 'Select a completed run in this schedule version.'}
    return start_mode, source_run, None


def _optimizer_authoritative_coverage_preflight(version, start_mode, source_run):
    """Report immutable assignment/request combinations that exceed capacity."""
    instances = list(
        ScheduleShiftInstance.objects.filter(
            schedule_version=version,
            date__gte=version.schedule_block.start_date,
            date__lte=version.schedule_block.end_date,
        )
        .select_related('facility', 'shift_template')
        .order_by('date', 'start_datetime', 'id')
    )
    instance_by_id = {instance.id: instance for instance in instances}
    instances_by_date_template = {}
    for instance in instances:
        instances_by_date_template.setdefault(
            (instance.date, instance.shift_template_id), [],
        ).append(instance)

    manual_only_physician_ids = set(
        ContractUserAssignment.objects.filter(
            domain=version.domain,
            contract__active=True,
            contract__manual_assignment_only=True,
            physician__active=True,
        ).values_list('physician_id', flat=True)
    )
    raw_assignments = list(
        assignments_for_viewed_run(version, source_run)
        .select_related('shift_instance', 'physician__user')
    )
    normalized_assignments, _normalization = canonical_assignment_snapshot(
        raw_assignments,
        instances,
        selected_run=source_run,
        preserve_physician_ids=manual_only_physician_ids,
    )
    normalized_assignments = [
        assignment
        for assignment in normalized_assignments
        if not (
            assignment.physician_id in manual_only_physician_ids
            and assignment.assignment_source
            == ScheduleShiftAssignment.AssignmentSource.OPTIMIZER
        )
    ]

    contributors_by_instance = {}

    def physician_name(physician):
        return (
            physician.user.get_full_name()
            or physician.display_name
            or physician.user.username
        )

    for assignment in normalized_assignments:
        if assignment.shift_instance_id not in instance_by_id:
            continue
        # A previous run is only the optimizer's starting state. Unlocked
        # optimizer assignments remain movable and must yield to newer
        # authoritative Shift On requests. Only assignments that are actually
        # fixed should participate in the capacity preflight.
        include = (
            assignment.is_locked
            or assignment.physician_id in manual_only_physician_ids
        )
        if not include:
            continue
        key = (assignment.shift_instance_id, assignment.physician_id)
        contributors_by_instance.setdefault(assignment.shift_instance_id, {})[key] = {
            'physician_id': assignment.physician_id,
            'physician': physician_name(assignment.physician),
            'sources': [{
                'type': 'LOCKED_ASSIGNMENT' if assignment.is_locked else 'MANUAL_ONLY_ASSIGNMENT',
                'label': (
                    'Locked manual assignment'
                    if assignment.is_locked
                    else 'Manual-only assignment'
                ),
                'assignment_id': assignment.id,
            }],
        }

    requests = list(
        ScheduleRequest.objects.filter(
            schedule_block=version.schedule_block,
            physician_id__in=manual_only_physician_ids,
            request_type=ScheduleRequest.RequestType.SHIFT_ON,
            date__gte=version.schedule_block.start_date,
            date__lte=version.schedule_block.end_date,
        )
        .select_related('physician__user')
        .prefetch_related('shift_templates')
        .order_by('date', 'id')
    )
    unresolved_requests = []
    for schedule_request in requests:
        matching_instances = []
        for template in schedule_request.shift_templates.all():
            matching_instances.extend(
                instances_by_date_template.get(
                    (schedule_request.date, template.id), (),
                )
            )
        matching_instances.sort(
            key=lambda item: (item.start_datetime, item.end_datetime, item.id)
        )
        if not matching_instances:
            unresolved_requests.append({
                'request_id': schedule_request.id,
                'physician_id': schedule_request.physician_id,
                'physician': physician_name(schedule_request.physician),
                'date': schedule_request.date.isoformat(),
                'reason': 'No matching dated shift exists.',
            })
            continue
        instance = matching_instances[0]
        key = (instance.id, schedule_request.physician_id)
        contributor = contributors_by_instance.setdefault(instance.id, {}).setdefault(
            key,
            {
                'physician_id': schedule_request.physician_id,
                'physician': physician_name(schedule_request.physician),
                'sources': [],
            },
        )
        contributor['sources'].append({
            'type': 'AUTHORITATIVE_SHIFT_ON_REQUEST',
            'label': 'Authoritative manual-only Shift On request',
            'request_id': schedule_request.id,
            'request_scope': schedule_request.request_scope,
            'request_weight': schedule_request.weight,
        })

    conflicts = []
    for instance_id, contributors_by_pair in contributors_by_instance.items():
        instance = instance_by_id[instance_id]
        contributors = list(contributors_by_pair.values())
        if len(contributors) <= instance.required_staffing:
            continue
        conflicts.append({
            'shift_instance_id': instance.id,
            'date': instance.date.isoformat(),
            'facility': instance.facility.name,
            'shift': instance.shift_template.name,
            'required_staffing': instance.required_staffing,
            'mandatory_assignments': len(contributors),
            'overstaffed_by': len(contributors) - instance.required_staffing,
            'contributors': contributors,
        })

    return {
        'status': 'conflict' if conflicts else 'ready',
        'has_conflicts': bool(conflicts),
        'conflict_count': len(conflicts),
        'conflicts': conflicts,
        'unresolved_requests': unresolved_requests,
        'start_mode': start_mode,
        'source_run_id': source_run.id if source_run is not None else None,
        'source_run_number': source_run.run_number if source_run is not None else None,
        'detail': (
            f'{len(conflicts)} shift(s) have more mandatory assignments than staffing capacity. '
            'Resolve the highlighted locked assignments and authoritative requests before optimizing or scoring.'
            if conflicts
            else 'No mandatory assignment capacity conflicts were found.'
        ),
    }


def _optimizer_preflight_response(version, start_mode, source_run):
    report = _optimizer_authoritative_coverage_preflight(
        version, start_mode, source_run,
    )
    if report['has_conflicts']:
        return Response(
            {'detail': report['detail'], 'optimizer_preflight': report},
            status=status.HTTP_409_CONFLICT,
        )
    return None


@api_view(['POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_version_optimizer_preflight(request, version_id):
    version = get_object_or_404(
        ScheduleVersion.objects.select_related('schedule_block', 'domain'),
        id=version_id,
    )
    if not _can_manage_build_workspace(request.user, version.domain):
        return _build_workspace_forbidden_response()
    start_mode, source_run, start_error = _optimizer_start_options(request, version)
    if start_error:
        return Response(start_error, status=status.HTTP_400_BAD_REQUEST)
    return Response(_optimizer_authoritative_coverage_preflight(
        version, start_mode, source_run,
    ))


def _run_optimizer_response(request, version):
    seed, seed_error = _parse_optimizer_seed(request)
    if seed_error:
        return Response(seed_error, status=status.HTTP_400_BAD_REQUEST)
    max_runtime_seconds, runtime_error = _parse_optimizer_max_runtime_seconds(request)
    if runtime_error:
        return Response(runtime_error, status=status.HTTP_400_BAD_REQUEST)
    optimization_focus, focus_error = _parse_optimizer_focus(request)
    if focus_error:
        return Response(focus_error, status=status.HTTP_400_BAD_REQUEST)
    optimizer_engine, engine_error = _parse_optimizer_engine(request)
    if engine_error:
        return Response(engine_error, status=status.HTTP_400_BAD_REQUEST)
    start_mode, source_run, start_error = _optimizer_start_options(request, version)
    if start_error:
        return Response(start_error, status=status.HTTP_400_BAD_REQUEST)
    if (
        optimizer_engine == 'V2'
        and not getattr(settings, 'ATLAS_V2_ENABLED', False)
    ):
        return Response({
            'optimizer_engine': 'Atlas V2 is not available.',
        }, status=status.HTTP_409_CONFLICT)
    if optimizer_engine == 'V2':
        return Response({
            'optimizer_engine': 'Atlas V2 must run in the background.',
        }, status=status.HTTP_400_BAD_REQUEST)
    preflight_response = _optimizer_preflight_response(
        version, start_mode, source_run,
    )
    if preflight_response is not None:
        return preflight_response
    # This legacy request-bound path still performs shared-state activation and
    # therefore remains single-flight. Parallel searches use the background
    # endpoint, where every worker runs against an isolated run snapshot.
    running_run = _blocking_optimizer_run(version)
    if running_run is not None:
        return Response({
            'detail': 'An optimizer run is already running for this schedule version.',
            'optimizer_run_ids': [running_run.id],
            'optimizer_run_id': running_run.id,
            'optimizer_concurrency_limit': 1,
        }, status=status.HTTP_409_CONFLICT)
    control = None
    token = request.data.get('search_token')
    if token:
        try:
            token = UUID(str(token))
        except ValueError:
            return Response({'detail': 'Invalid search token.'}, status=400)
        OptimizerControl.objects.filter(
            schedule_version=version,
            started_at__isnull=True,
            created_at__lt=timezone.now() - timedelta(minutes=15),
        ).delete()
        try:
            with transaction.atomic():
                control = OptimizerControl.objects.create(token=token, schedule_version=version, created_by=request.user)
        except IntegrityError:
            return Response({'detail': 'An optimizer search is already running.'}, status=409)
    elif OptimizerControl.objects.filter(schedule_version=version).exists():
        return Response({'detail': 'An optimizer search is already running.'}, status=409)
    last_poll, requested = [0.0], [False]

    def stop_requested():
        now = monotonic()
        if control is not None and now - last_poll[0] >= 0.5:
            requested[0] = OptimizerControl.objects.filter(token=control.token, stop_requested=True).exists()
            last_poll[0] = now
        return requested[0]

    try:
        summary = optimize_schedule_version(
            version, created_by=request.user, seed=seed,
            start_mode=start_mode, source_run=source_run,
            max_runtime_seconds=max_runtime_seconds,
            optimization_focus=optimization_focus,
            adaptive_runtime=control is not None, stop_requested=stop_requested,
        )
    except ValueError as optimizer_error:
        return Response({'detail': str(optimizer_error)}, status=status.HTTP_400_BAD_REQUEST)
    finally:
        if control is not None:
            OptimizerControl.objects.filter(token=control.token).delete()
    return Response(summary)


@api_view(['POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_version_stop_optimizer(request, version_id):
    version = get_object_or_404(
        ScheduleVersion.objects.select_related('domain'), id=version_id,
    )
    if not _can_manage_build_workspace(request.user, version.domain):
        return _build_workspace_forbidden_response()
    run_id = request.data.get('optimizer_run_id')
    controls = OptimizerControl.objects.filter(
        schedule_version_id=version_id,
        created_by=request.user,
    )
    if run_id not in (None, ''):
        try:
            controls = controls.filter(optimizer_run_id=int(run_id))
        except (TypeError, ValueError):
            return Response({'detail': 'Invalid optimizer run.'}, status=400)
    else:
        try:
            controls = controls.filter(token=UUID(str(request.data.get('search_token', ''))))
        except ValueError:
            return Response({'detail': 'Invalid optimizer run.'}, status=400)
    updated = controls.update(stop_requested=True)
    if not updated:
        return Response({'detail': 'This search has finished or is not yet ready to stop.'}, status=409)
    return Response({'detail': 'Stop requested. Finishing the current check and preserving the best valid schedule.'})


@api_view(['POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_version_optimize(request, block_id, version_id):
    block = get_object_or_404(ScheduleBlock.objects.select_related('domain'), id=block_id)
    version = get_object_or_404(
        ScheduleVersion.objects.select_related('schedule_block', 'domain'),
        id=version_id,
        schedule_block=block,
    )
    if not _can_manage_build_workspace(request.user, version.domain):
        return _build_workspace_forbidden_response()
    return _run_optimizer_response(request, version)


@api_view(['GET'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_version_optimizer_runs(request, version_id):
    version = get_object_or_404(ScheduleVersion.objects.select_related('domain'), id=version_id)
    if not _can_manage_build_workspace(request.user, version.domain):
        return _build_workspace_forbidden_response()
    _cleanup_stale_optimizer_runs(version)
    runs = OptimizerRun.objects.filter(schedule_version=version).order_by('-run_number')
    return Response(OptimizerRunSerializer(runs, many=True).data)


@api_view(['POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_version_run_optimizer(request, version_id):
    version = get_object_or_404(
        ScheduleVersion.objects.select_related('schedule_block', 'domain'),
        id=version_id,
    )
    if not _can_manage_build_workspace(request.user, version.domain):
        return _build_workspace_forbidden_response()
    if not request.data.get('background'):
        return _run_optimizer_response(request, version)
    seed, seed_error = _parse_optimizer_seed(request)
    if seed_error:
        return Response(seed_error, status=status.HTTP_400_BAD_REQUEST)
    max_runtime_seconds, runtime_error = _parse_optimizer_max_runtime_seconds(request)
    if runtime_error:
        return Response(runtime_error, status=status.HTTP_400_BAD_REQUEST)
    optimization_focus, focus_error = _parse_optimizer_focus(request)
    if focus_error:
        return Response(focus_error, status=status.HTTP_400_BAD_REQUEST)
    optimizer_engine, engine_error = _parse_optimizer_engine(request)
    if engine_error:
        return Response(engine_error, status=status.HTTP_400_BAD_REQUEST)
    start_mode, source_run, start_error = _optimizer_start_options(request, version)
    if start_error:
        return Response(start_error, status=status.HTTP_400_BAD_REQUEST)
    if (
        optimizer_engine == 'V2'
        and not getattr(settings, 'ATLAS_V2_ENABLED', False)
    ):
        return Response({
            'optimizer_engine': 'Atlas V2 is not available.',
        }, status=status.HTTP_409_CONFLICT)
    preflight_response = _optimizer_preflight_response(
        version, start_mode, source_run,
    )
    if preflight_response is not None:
        return preflight_response
    if (
        version.status != ScheduleVersion.Status.BUILD
        or version.schedule_block.build_status != ScheduleBlock.BuildStatus.BUILD
    ):
        return Response(
            {'detail': 'Optimizer can only run on a BUILD Schedule Version.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    token = request.data.get('search_token')
    try:
        token = UUID(str(token)) if token else UUID(int=secrets.randbits(128))
    except ValueError:
        return Response({'detail': 'Invalid search token.'}, status=400)
    OptimizerControl.objects.filter(
        schedule_version=version,
        started_at__isnull=True,
        created_at__lt=timezone.now() - timedelta(minutes=15),
    ).delete()
    try:
        with transaction.atomic():
            locked_version = ScheduleVersion.objects.select_for_update().get(id=version.id)
            stale_controls = OptimizerControl.objects.filter(
                schedule_version=locked_version,
                started_at__isnull=True,
                created_at__lt=timezone.now() - timedelta(minutes=15),
            )
            stale_run_ids = list(
                stale_controls.exclude(optimizer_run_id=None).values_list(
                    'optimizer_run_id', flat=True,
                )
            )
            stale_controls.delete()
            if stale_run_ids:
                OptimizerRun.objects.filter(
                    id__in=stale_run_ids,
                    status=OptimizerRun.Status.RUNNING,
                ).update(
                    status=OptimizerRun.Status.FAILED,
                    is_active=False,
                    notes='Optimizer queue entry expired before a worker claimed it.',
                )
            running_runs = list(
                OptimizerRun.objects.filter(
                    schedule_version=locked_version,
                    status=OptimizerRun.Status.RUNNING,
                ).order_by('run_number').values_list('id', flat=True)
            )
            concurrency_limit = _optimizer_concurrency_limit()
            if len(running_runs) >= concurrency_limit:
                return Response({
                    'detail': (
                        f'This schedule version already has {len(running_runs)} optimizer '
                        f'run(s) in progress; the limit is {concurrency_limit}.'
                    ),
                    'optimizer_run_ids': running_runs,
                    'optimizer_run_id': running_runs[0],
                    'optimizer_concurrency_limit': concurrency_limit,
                }, status=status.HTTP_409_CONFLICT)
            latest_number = (
                OptimizerRun.objects.filter(schedule_version=locked_version)
                .order_by('-run_number').values_list('run_number', flat=True).first() or 0
            )
            locked_open_ids = (
                list(source_run.locked_open_shift_instance_ids or [])
                if source_run is not None else list(
                    ScheduleShiftInstance.objects.filter(
                        schedule_version=locked_version, is_locked_open=True,
                    ).values_list('id', flat=True)
                )
            )
            optimizer_run = OptimizerRun.objects.create(
                schedule_version=locked_version,
                run_number=latest_number + 1,
                created_by=request.user,
                status=OptimizerRun.Status.RUNNING,
                seed=seed if seed is not None else secrets.randbits(63),
                start_mode=start_mode,
                started_from_run=(
                    source_run
                    if start_mode == OptimizerRun.StartMode.CURRENT_SCHEDULE
                    else None
                ),
                started_from_run_number=(
                    source_run.run_number
                    if start_mode == OptimizerRun.StartMode.CURRENT_SCHEDULE
                    and source_run is not None
                    else None
                ),
                initial_score=(
                    source_run.final_score
                    if start_mode == OptimizerRun.StartMode.CURRENT_SCHEDULE
                    and source_run is not None
                    else None
                ),
                max_runtime_seconds=max_runtime_seconds or 120 * 60,
                optimization_focus=optimization_focus,
                run_kind=(
                    V2_RUN_KIND
                    if optimizer_engine == 'V2'
                    else 'OPTIMIZER'
                ),
                locked_open_shift_instance_ids=locked_open_ids,
            )
            OptimizerControl.objects.create(
                token=token,
                schedule_version=locked_version,
                created_by=request.user,
                optimizer_run=optimizer_run,
                source_run=source_run,
            )
    except IntegrityError:
        return Response(
            {'detail': 'The optimizer run could not be numbered safely. Please try again.'},
            status=409,
        )
    payload = OptimizerRunSerializer(optimizer_run).data
    payload['message'] = 'Optimizer queued. You may leave this page; the search will continue.'
    return Response(payload, status=status.HTTP_202_ACCEPTED)


@api_view(['POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_version_recalculate_score(request, version_id):
    version = get_object_or_404(
        ScheduleVersion.objects.select_related('schedule_block', 'domain'),
        id=version_id,
    )
    if not _can_manage_build_workspace(request.user, version.domain):
        return _build_workspace_forbidden_response()
    if (
        version.schedule_block.build_status != ScheduleBlock.BuildStatus.BUILD
        or version.status != ScheduleVersion.Status.BUILD
    ):
        return Response(
            {'detail': 'Scores can only be recalculated in a BUILD Schedule Version.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    optimizer_run, run_error = _requested_editable_run(request, version)
    if run_error:
        return run_error
    if optimizer_run is None:
        return Response(
            {'detail': 'Select the viewed active optimizer run to recalculate its score.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    preflight_response = _optimizer_preflight_response(
        version, OptimizerRun.StartMode.CURRENT_SCHEDULE, optimizer_run,
    )
    if preflight_response is not None:
        return preflight_response
    summary, report = recalculate_schedule_version_score(version, optimizer_run)
    return Response({
        'optimizer_summary': summary,
        'optimizer_run': OptimizerRunSerializer(
            OptimizerRun.objects.get(id=optimizer_run.id)
        ).data,
        'violation_report': report,
    })


@api_view(['POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def optimizer_run_save_copy(request, run_id):
    source = get_object_or_404(
        OptimizerRun.objects.select_related('schedule_version__schedule_block', 'schedule_version__domain'),
        id=run_id, status=OptimizerRun.Status.COMPLETED,
    )
    version = source.schedule_version
    if not _can_manage_build_workspace(request.user, version.domain):
        return _build_workspace_forbidden_response()
    if version.status != ScheduleVersion.Status.BUILD or version.schedule_block.build_status != ScheduleBlock.BuildStatus.BUILD:
        return Response({'detail': 'Copies can only be saved in a BUILD Schedule Version.'}, status=status.HTTP_400_BAD_REQUEST)

    with transaction.atomic():
        version = ScheduleVersion.objects.select_for_update().get(id=version.id)
        source = OptimizerRun.objects.select_for_update().get(id=source.id)
        latest_number = OptimizerRun.objects.filter(schedule_version=version).order_by('-run_number').values_list('run_number', flat=True).first() or 0
        locked_open_ids = list(
            ScheduleShiftInstance.objects.filter(schedule_version=version, is_locked_open=True)
            .values_list('id', flat=True)
        ) if source.is_active else list(source.locked_open_shift_instance_ids or [])
        OptimizerRun.objects.filter(schedule_version=version, is_active=True).update(is_active=False)
        copied = OptimizerRun.objects.create(
            schedule_version=version,
            run_number=latest_number + 1,
            created_by=request.user,
            status=OptimizerRun.Status.COMPLETED,
            seed=source.seed,
            initial_score=source.initial_score,
            final_score=source.final_score,
            score_breakdown=source.score_breakdown,
            optimizer_summary=source.optimizer_summary,
            optimizer_debug=source.optimizer_debug,
            notes=f'Copy of Run {source.run_number}',
            is_active=True,
            score_is_stale=source.score_is_stale,
            copied_from_run=source,
            run_kind='COPY',
            locked_open_shift_instance_ids=locked_open_ids,
            start_mode=source.start_mode,
            optimization_focus=source.optimization_focus,
        )
        source_assignments = ScheduleShiftAssignment.objects.filter(
            visible_assignment_filter(source),
            shift_instance__schedule_version=version,
        )
        source_instances = list(
            ScheduleShiftInstance.objects.filter(schedule_version=version)
        )
        source_assignments, _normalization = canonical_assignment_snapshot(
            list(source_assignments), source_instances, selected_run=source,
        )
        ScheduleShiftAssignment.objects.bulk_create([
            ScheduleShiftAssignment(
                shift_instance_id=row.shift_instance_id,
                physician_id=row.physician_id,
                created_by=request.user,
                assignment_source=row.assignment_source,
                optimizer_run=copied,
                is_locked=row.is_locked,
            )
            for row in source_assignments
        ])
        ScheduleShiftInstance.objects.filter(schedule_version=version).update(is_locked_open=False)
        ScheduleShiftInstance.objects.filter(id__in=locked_open_ids, schedule_version=version).update(is_locked_open=True)
        copied.optimizer_summary = {**(copied.optimizer_summary or {}), 'optimizer_run_id': copied.id, 'optimizer_run_number': copied.run_number}
        copied.save(update_fields=['optimizer_summary'])
        version.optimizer_summary = copied.optimizer_summary
        version.score_is_stale = copied.score_is_stale
        version.save(update_fields=['optimizer_summary', 'score_is_stale', 'updated_at'])
    return Response(OptimizerRunSerializer(copied).data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'DELETE'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def optimizer_run_detail(request, run_id):
    optimizer_run = get_object_or_404(
        OptimizerRun.objects.select_related('schedule_version__schedule_block', 'schedule_version__domain'),
        id=run_id,
    )
    if not _can_manage_build_workspace(request.user, optimizer_run.schedule_version.domain):
        return _build_workspace_forbidden_response()
    if request.method == 'DELETE':
        if (
            optimizer_run.schedule_version.status != ScheduleVersion.Status.BUILD
            or optimizer_run.schedule_version.schedule_block.build_status
            != ScheduleBlock.BuildStatus.BUILD
        ):
            return Response(
                {'detail': 'Optimizer runs can only be deleted while the schedule is in BUILD.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        viewed_run_id = request.query_params.get('viewed_run_id')
        try:
            viewed_run_id = int(viewed_run_id) if viewed_run_id not in (None, '') else None
        except (TypeError, ValueError):
            return Response(
                {'detail': 'viewed_run_id must be an integer.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if optimizer_run.is_active:
            return Response(
                {
                    'detail': 'Cannot delete active optimizer run. Activate another run first.',
                    'deleted_run_ids': [],
                    'skipped_run_ids': [{'id': optimizer_run.id, 'reason': 'active_run'}],
                    'next_viewed_run_id': viewed_run_id,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if viewed_run_id == optimizer_run.id:
            return Response(
                {
                    'detail': 'Cannot delete the currently viewed optimizer run. View another run first.',
                    'deleted_run_ids': [],
                    'skipped_run_ids': [{'id': optimizer_run.id, 'reason': 'viewed_run'}],
                    'next_viewed_run_id': viewed_run_id,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if OptimizerControl.objects.filter(
            source_run=optimizer_run,
            optimizer_run__status=OptimizerRun.Status.RUNNING,
        ).exists():
            return Response(
                {
                    'detail': 'Cannot delete a run while an optimizer is using it as its starting schedule.',
                    'deleted_run_ids': [],
                    'skipped_run_ids': [{'id': optimizer_run.id, 'reason': 'optimizer_source'}],
                    'next_viewed_run_id': viewed_run_id,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if optimizer_run.status == OptimizerRun.Status.RUNNING:
            _cleanup_stale_optimizer_runs(optimizer_run.schedule_version)
            optimizer_run.refresh_from_db()
            if optimizer_run.status == OptimizerRun.Status.RUNNING:
                return Response(
                    {
                        'detail': 'Cannot delete a running optimizer run until it is stale or failed.',
                        'deleted_run_ids': [],
                        'skipped_run_ids': [{'id': optimizer_run.id, 'reason': 'running'}],
                        'next_viewed_run_id': viewed_run_id,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
        version = optimizer_run.schedule_version
        with transaction.atomic():
            deleted_assignment_count, _ = ScheduleShiftAssignment.objects.filter(
                optimizer_run=optimizer_run,
            ).delete()
            optimizer_run.delete()
        next_viewed_run = (
            OptimizerRun.objects.filter(
                schedule_version=version,
                status=OptimizerRun.Status.COMPLETED,
                is_active=True,
            ).first()
            or OptimizerRun.objects.filter(
                schedule_version=version,
                status=OptimizerRun.Status.COMPLETED,
            ).order_by('-run_number').first()
        )
        return Response(
            {
                'message': f'Deleted optimizer run and {deleted_assignment_count} optimizer assignment(s).',
                'assignments_deleted': deleted_assignment_count,
                'deleted_run_ids': [run_id],
                'skipped_run_ids': [],
                'next_viewed_run_id': (
                    viewed_run_id
                    if viewed_run_id and OptimizerRun.objects.filter(
                        id=viewed_run_id, schedule_version=version,
                    ).exists()
                    else getattr(next_viewed_run, 'id', None)
                ),
            }
        )
    if request.query_params.get('compact') in {'1', 'true', 'TRUE'}:
        return Response(OptimizerRunHistorySerializer(optimizer_run).data)
    return Response(OptimizerRunSerializer(optimizer_run).data)


@api_view(['POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def optimizer_runs_bulk_delete(request, version_id):
    version = get_object_or_404(
        ScheduleVersion.objects.select_related('schedule_block', 'domain'), id=version_id,
    )
    if not _can_manage_build_workspace(request.user, version.domain):
        return _build_workspace_forbidden_response()
    if (
        version.status != ScheduleVersion.Status.BUILD
        or version.schedule_block.build_status != ScheduleBlock.BuildStatus.BUILD
    ):
        return Response(
            {'detail': 'Optimizer runs can only be deleted while the schedule is in BUILD.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    run_ids = request.data.get('run_ids')
    if not isinstance(run_ids, list) or not run_ids:
        return Response(
            {'detail': 'run_ids must be a non-empty list.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        requested_ids = list(dict.fromkeys(int(run_id) for run_id in run_ids))
        viewed_run_id = request.data.get('viewed_run_id')
        viewed_run_id = int(viewed_run_id) if viewed_run_id not in (None, '') else None
    except (TypeError, ValueError):
        return Response(
            {'detail': 'run_ids and viewed_run_id must contain integer IDs.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    deleted_ids = []
    skipped = []
    assignments_deleted = 0
    protected_source_run_ids = set(
        OptimizerControl.objects.filter(
            schedule_version=version,
            optimizer_run__status=OptimizerRun.Status.RUNNING,
            source_run_id__in=requested_ids,
        ).values_list('source_run_id', flat=True)
    )
    with transaction.atomic():
        # Do not lock the schedule version or a currently running row. The
        # optimizer owns those records for the duration of its search, while
        # completed inactive history rows are independent deletion targets.
        candidate_runs_by_id = {
            run.id: run
            for run in OptimizerRun.objects.filter(
                schedule_version=version, id__in=requested_ids,
            )
        }
        eligible_ids = [
            run_id
            for run_id, run in candidate_runs_by_id.items()
            if not run.is_active
            and run.id != viewed_run_id
            and run.status != OptimizerRun.Status.RUNNING
            and run_id not in protected_source_run_ids
        ]
        locked_eligible_runs = {
            run.id: run
            for run in OptimizerRun.objects.select_for_update().filter(
                schedule_version=version, id__in=eligible_ids,
            )
        }
        deletable_ids = []
        for run_id in requested_ids:
            run = candidate_runs_by_id.get(run_id)
            reason = None
            if run is None:
                reason = 'not_found_in_schedule_version'
            elif run.is_active:
                reason = 'active_run'
            elif run.id == viewed_run_id:
                reason = 'viewed_run'
            elif run.status == OptimizerRun.Status.RUNNING:
                reason = 'running'
            elif run.id in protected_source_run_ids:
                reason = 'optimizer_source'
            if reason:
                skipped.append({'id': run_id, 'reason': reason})
                continue
            run = locked_eligible_runs.get(run_id)
            if run is None:
                skipped.append({'id': run_id, 'reason': 'changed_during_delete'})
                continue
            if run.is_active or run.status == OptimizerRun.Status.RUNNING:
                skipped.append({
                    'id': run_id,
                    'reason': 'active_run' if run.is_active else 'running',
                })
                continue
            deletable_ids.append(run_id)

        if deletable_ids:
            assignments = ScheduleShiftAssignment.objects.filter(
                optimizer_run_id__in=deletable_ids,
            )
            assignments_deleted = assignments.count()
            assignments.delete()
            OptimizerRun.objects.filter(id__in=deletable_ids).delete()
            deleted_ids.extend(deletable_ids)

    next_viewed_run_id = viewed_run_id
    if not (
        next_viewed_run_id
        and OptimizerRun.objects.filter(
            id=next_viewed_run_id, schedule_version=version,
        ).exists()
    ):
        next_run = (
            OptimizerRun.objects.filter(
                schedule_version=version,
                status=OptimizerRun.Status.COMPLETED,
                is_active=True,
            ).first()
            or OptimizerRun.objects.filter(
                schedule_version=version,
                status=OptimizerRun.Status.COMPLETED,
            ).order_by('-run_number').first()
        )
        next_viewed_run_id = getattr(next_run, 'id', None)
    return Response({
        'message': f'Deleted {len(deleted_ids)} optimizer run(s).',
        'deleted_run_ids': deleted_ids,
        'skipped_run_ids': skipped,
        'assignments_deleted': assignments_deleted,
        'next_viewed_run_id': next_viewed_run_id,
    })


@api_view(['POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def optimizer_run_activate(request, run_id):
    optimizer_run = get_object_or_404(
        OptimizerRun.objects.select_related('schedule_version__schedule_block', 'schedule_version__domain'),
        id=run_id,
    )
    if not _can_manage_build_workspace(request.user, optimizer_run.schedule_version.domain):
        return _build_workspace_forbidden_response()
    if optimizer_run.schedule_version.schedule_block.published_at is not None:
        return Response(
            {'detail': 'Unpublish this Schedule Block before activating another optimizer run.'},
            status=status.HTTP_409_CONFLICT,
        )
    if optimizer_run.status != OptimizerRun.Status.COMPLETED:
        return Response(
            {'detail': 'Only completed optimizer runs can be activated.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    with transaction.atomic():
        OptimizerRun.objects.filter(
            schedule_version=optimizer_run.schedule_version,
            is_active=True,
        ).exclude(id=optimizer_run.id).update(is_active=False)
        optimizer_run.is_active = True
        optimizer_run.save(update_fields=['is_active'])
        version = optimizer_run.schedule_version
        ScheduleShiftInstance.objects.filter(schedule_version=version).update(is_locked_open=False)
        ScheduleShiftInstance.objects.filter(
            schedule_version=version,
            id__in=optimizer_run.locked_open_shift_instance_ids or [],
        ).update(is_locked_open=True)
        version.optimizer_summary = optimizer_run.optimizer_summary
        version.save(update_fields=['optimizer_summary', 'updated_at'])
    return Response(OptimizerRunSerializer(optimizer_run).data)


def _published_violation_report(version):
    """Return publication-time scoring without consulting current rules."""
    if version.published_violation_report:
        return version.published_violation_report

    published_run = version.published_optimizer_run
    breakdown = dict(published_run.score_breakdown or {}) if published_run else {}
    total_score = float(published_run.final_score or 0) if published_run else 0.0
    return {
        'schedule_version': {
            'id': version.id,
            'schedule_block': version.schedule_block_id,
            'domain': version.domain_id,
            'domain_name': version.domain.name,
            'version_number': version.version_number,
            'name': version.name,
            'status': version.status,
        },
        'schedule_block': {
            'id': version.schedule_block_id,
            'name': version.schedule_block.generated_name,
            'start_date': version.schedule_block.start_date.isoformat(),
            'end_date': version.schedule_block.end_date.isoformat(),
        },
        'optimizer_run': (
            {
                'id': published_run.id,
                'schedule_version': published_run.schedule_version_id,
                'run_number': published_run.run_number,
                'created_at': published_run.created_at.isoformat(),
                'status': published_run.status,
                'initial_score': float(published_run.initial_score) if published_run.initial_score is not None else None,
                'final_score': float(published_run.final_score) if published_run.final_score is not None else None,
                'is_active': published_run.is_active,
                'score_is_stale': False,
            }
            if published_run is not None
            else None
        ),
        'total_score': total_score,
        'score_breakdown': breakdown,
        'warnings': [
            'This legacy publication predates detailed scoring snapshots. The stored published score is shown without reevaluating current contracts or requests.'
        ],
        'fixed_request_feasibility': {},
        'score_audit': {},
        'debug': {'publication_snapshot': True, 'legacy_snapshot': True},
        'users': [],
    }


@api_view(['GET'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_version_violation_report(request, version_id):
    version = get_object_or_404(
        ScheduleVersion.objects.select_related(
            'schedule_block', 'domain', 'published_optimizer_run',
        ),
        id=version_id,
    )
    if not _can_manage_build_workspace(request.user, version.domain):
        return _build_workspace_forbidden_response()
    if version.schedule_block.published_at is not None:
        requested_run_id = request.query_params.get('optimizer_run_id')
        if requested_run_id not in (None, '') and str(
            version.published_optimizer_run_id or ''
        ) != str(requested_run_id):
            return Response(
                {'detail': 'Published schedules can only show the frozen published-run report.'},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(_published_violation_report(version))
    optimizer_run = _get_optimizer_run_for_version(version, request.query_params.get('optimizer_run_id'))
    report = build_violation_report(version, optimizer_run=optimizer_run)
    report['requests'] = []

    requested_physician_id = request.query_params.get('physician_id')
    if requested_physician_id:
        try:
            requested_physician_id = int(requested_physician_id)
        except (TypeError, ValueError):
            return Response(
                {'physician_id': 'physician_id must be a valid integer.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        get_object_or_404(Physician, id=requested_physician_id)
        request_items = (
            ScheduleRequest.objects.filter(
                schedule_block=version.schedule_block,
                physician_id=requested_physician_id,
                date__gte=version.schedule_block.start_date,
                date__lte=version.schedule_block.end_date,
            )
            .select_related('physician__user')
            .prefetch_related('shift_templates__facility')
        )
        report['requests'] = ScheduleRequestSerializer(request_items, many=True).data

    return Response(report)


@api_view(['GET'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def optimizer_run_violations(request, run_id):
    optimizer_run = get_object_or_404(
        OptimizerRun.objects.select_related(
            'schedule_version__schedule_block',
            'schedule_version__domain',
            'schedule_version__published_optimizer_run',
        ),
        id=run_id,
    )
    if not _can_manage_build_workspace(request.user, optimizer_run.schedule_version.domain):
        return _build_workspace_forbidden_response()
    if optimizer_run.schedule_version.schedule_block.published_at is not None:
        if optimizer_run.schedule_version.published_optimizer_run_id != optimizer_run.id:
            return Response(
                {'detail': 'Published schedules can only show the frozen published-run report.'},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(_published_violation_report(optimizer_run.schedule_version))
    return Response(build_violation_report(optimizer_run.schedule_version, optimizer_run=optimizer_run))


def _schedule_version_assignment_summary(version, message, cleared_count=0):
    active_run = _active_optimizer_run(version)
    instances = list(
        ScheduleShiftInstance.objects.filter(
            schedule_version=version,
            date__gte=version.schedule_block.start_date,
            date__lte=version.schedule_block.end_date,
        )
        .prefetch_related('assignments')
    )
    unfilled_shift_count = sum(
        max(instance.required_staffing - instance.assignments.filter(visible_assignment_filter(active_run)).count(), 0)
        for instance in instances
    )
    return {
        'message': message,
        'assignments_cleared': cleared_count,
        'total_score': 0,
        'unfilled_shift_count': unfilled_shift_count,
        'assignments_made': 0,
        'request_violations_summary': {
            'violations': 0,
            'rewards': 0,
        },
        'rest_violations_blocked': 0,
        'debug': {
            'schedule_version_id': version.id,
            'schedule_block_id': version.schedule_block_id,
            'schedule_block_start_date': version.schedule_block.start_date.isoformat(),
            'schedule_block_end_date': version.schedule_block.end_date.isoformat(),
            'shift_instances_considered': len(instances),
        },
        'workload_summary': [],
    }


@api_view(['POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_version_clear_optimizer_assignments(request, block_id, version_id):
    block = get_object_or_404(ScheduleBlock.objects.select_related('domain'), id=block_id)
    version = get_object_or_404(
        ScheduleVersion.objects.select_related('schedule_block', 'domain'),
        id=version_id,
        schedule_block=block,
    )
    if not _can_manage_build_workspace(request.user, version.domain):
        return _build_workspace_forbidden_response()
    if (
        block.build_status != ScheduleBlock.BuildStatus.BUILD
        or version.status != ScheduleVersion.Status.BUILD
    ):
        return Response(
            {'detail': 'Assignments can only be cleared in a BUILD Schedule Version.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    with transaction.atomic():
        active_run = _active_optimizer_run(version)
        if active_run is None:
            return Response(
                _schedule_version_assignment_summary(
                    version,
                    'No active optimizer run assignments to clear.',
                    0,
                )
            )
        affected_instance_ids = list(
            ScheduleShiftInstance.objects.filter(
                schedule_version=version,
                assignments__assignment_source=ScheduleShiftAssignment.AssignmentSource.OPTIMIZER,
                assignments__optimizer_run=active_run,
            )
            .distinct()
            .values_list('id', flat=True)
        )
        affected_instances = list(
            ScheduleShiftInstance.objects.select_for_update()
            .filter(id__in=affected_instance_ids)
        )
        cleared_count, _ = ScheduleShiftAssignment.objects.filter(
            shift_instance__schedule_version=version,
            assignment_source=ScheduleShiftAssignment.AssignmentSource.OPTIMIZER,
            optimizer_run=active_run,
        ).delete()
        for instance in affected_instances:
            _sync_shift_instance_status(instance)
        active_run.is_active = False
        active_run.notes = (active_run.notes + '\n' if active_run.notes else '') + 'Active optimizer assignments were cleared.'
        active_run.save(update_fields=['is_active', 'notes'])
        version.optimizer_summary = {}
        version.save(update_fields=['optimizer_summary', 'updated_at'])

    return Response(
        _schedule_version_assignment_summary(
            version,
            f'Cleared {cleared_count} optimizer-generated assignment(s).',
            cleared_count,
        )
    )


@api_view(['POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_version_clear_all_assignments(request, block_id, version_id):
    block = get_object_or_404(ScheduleBlock.objects.select_related('domain'), id=block_id)
    version = get_object_or_404(
        ScheduleVersion.objects.select_related('schedule_block', 'domain'),
        id=version_id,
        schedule_block=block,
    )
    if not _can_manage_build_workspace(request.user, version.domain):
        return _build_workspace_forbidden_response()
    if (
        block.build_status != ScheduleBlock.BuildStatus.BUILD
        or version.status != ScheduleVersion.Status.BUILD
    ):
        return Response(
            {'detail': 'Assignments can only be cleared in a BUILD Schedule Version.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    with transaction.atomic():
        affected_instance_ids = list(
            ScheduleShiftInstance.objects
            .filter(schedule_version=version, assignments__isnull=False)
            .distinct()
            .values_list('id', flat=True)
        )
        affected_instances = list(
            ScheduleShiftInstance.objects.select_for_update()
            .filter(id__in=affected_instance_ids)
        )
        cleared_count, _ = ScheduleShiftAssignment.objects.filter(
            shift_instance__schedule_version=version,
        ).delete()
        for instance in affected_instances:
            _sync_shift_instance_status(instance)
        OptimizerRun.objects.filter(schedule_version=version, is_active=True).update(is_active=False)
        version.optimizer_summary = {}
        version.save(update_fields=['optimizer_summary', 'updated_at'])

    return Response(
        _schedule_version_assignment_summary(
            version,
            f'Cleared {cleared_count} assignment(s).',
            cleared_count,
        )
    )


def _sync_shift_instance_status(instance):
    assigned_count = instance.assignments.filter(
        visible_assignment_filter(_active_optimizer_run(instance.schedule_version))
    ).count()
    next_status = (
        ScheduleShiftInstance.Status.ASSIGNED
        if assigned_count >= instance.required_staffing
        else ScheduleShiftInstance.Status.OPEN
    )
    if instance.status != next_status:
        instance.status = next_status
        instance.save(update_fields=['status', 'updated_at'])


def _format_shift_instance_assignment_label(shift_instance):
    start_label = shift_instance.start_datetime.strftime('%I:%M%p').lstrip('0').lower()
    end_label = shift_instance.end_datetime.strftime('%I:%M%p').lstrip('0').lower()
    start_label = start_label.replace(':00', '')
    end_label = end_label.replace(':00', '')
    start_label = start_label.replace('am', 'a').replace('pm', 'p')
    end_label = end_label.replace('am', 'a').replace('pm', 'p')
    facility_label = shift_instance.facility.short_name or shift_instance.facility.name
    return f'{facility_label} {start_label}-{end_label}'


def _physician_display_name(physician):
    return physician.display_name or physician.user.get_full_name() or physician.user.username


def _overlapping_assignment_for_physician(physician, shift_instance, exclude_assignment_id=None):
    active_run = _active_optimizer_run(shift_instance.schedule_version)
    query = ScheduleShiftAssignment.objects.filter(
            visible_assignment_filter(active_run),
            physician=physician,
            shift_instance__schedule_version=shift_instance.schedule_version,
            shift_instance__start_datetime__lt=shift_instance.end_datetime,
            shift_instance__end_datetime__gt=shift_instance.start_datetime,
        )
    if exclude_assignment_id is not None:
        query = query.exclude(id=exclude_assignment_id)
    else:
        query = query.exclude(shift_instance=shift_instance)
    return (
        query
        .select_related(
            'shift_instance__facility',
            'shift_instance__schedule_version',
            'physician__user',
        )
        .order_by('shift_instance__start_datetime', 'shift_instance__id')
        .first()
    )


def _overlapping_assignment_message(physician, overlapping_assignment):
    physician_name = _physician_display_name(physician)
    shift_label = _format_shift_instance_assignment_label(
        overlapping_assignment.shift_instance
    )
    return (
        f'{physician_name} is already assigned to {shift_label}, '
        f'which overlaps this shift.'
    )


def _physician_assignment_eligibility(physician, shift_instance):
    contract_assignment = (
        ContractUserAssignment.objects.filter(
            physician=physician,
            domain=shift_instance.schedule_version.domain,
            contract__active=True,
        )
        .select_related('contract', 'domain')
        .prefetch_related('contract__facilities')
        .first()
    )
    domain_eligible = contract_assignment is not None
    facility_eligible = bool(
        contract_assignment
        and contract_assignment.contract.facilities.filter(
            id=shift_instance.facility_id,
        ).exists()
    )
    can_assign = physician.active and domain_eligible and facility_eligible

    if not physician.active:
        reason = 'Physician is inactive.'
    elif not domain_eligible:
        reason = (
            f'No active Contract assignment in '
            f'{shift_instance.schedule_version.domain.name}.'
        )
    elif not facility_eligible:
        reason = f'Contract does not include {shift_instance.facility.name}.'
    else:
        overlapping_assignment = _overlapping_assignment_for_physician(
            physician,
            shift_instance,
        )
        if overlapping_assignment:
            reason = _overlapping_assignment_message(physician, overlapping_assignment)
            can_assign = False
        else:
            reason = ''

    return {
        'domain_eligible': domain_eligible,
        'facility_eligible': facility_eligible,
        'can_assign': can_assign,
        'ineligibility_reason': reason,
    }


def _assignment_context_payload(shift_instance):
    active_run = _active_optimizer_run(shift_instance.schedule_version)
    shift_instance = (
        ScheduleShiftInstance.objects.select_related(
            'facility',
            'shift_template',
            'schedule_version__domain',
            'schedule_block',
        )
        .prefetch_related('assignments__physician__user')
        .get(id=shift_instance.id)
    )
    assigned_physician_ids = set(
        shift_instance.assignments.filter(
            visible_assignment_filter(active_run)
        ).values_list('physician_id', flat=True)
    )
    eligible_physicians = []
    for physician in Physician.objects.filter(active=True).select_related('user').order_by(
        'user__last_name',
        'user__first_name',
        'id',
    ):
        display_name = _physician_display_name(physician)
        eligibility = _physician_assignment_eligibility(physician, shift_instance)
        eligible_physicians.append(
            {
                'id': physician.id,
                'name': display_name,
                'already_assigned': physician.id in assigned_physician_ids,
                **eligibility,
            }
        )
    listed_ids = {item['id'] for item in eligible_physicians}
    for assignment in shift_instance.assignments.filter(visible_assignment_filter(active_run)):
        physician = assignment.physician
        if physician.id not in listed_ids:
            eligibility = _physician_assignment_eligibility(physician, shift_instance)
            eligible_physicians.append({
                'id': physician.id,
                'name': _physician_display_name(physician),
                'already_assigned': True,
                **eligibility,
            })

    return {
        'shift_instance': ScheduleShiftInstanceSerializer(
            shift_instance,
            context={'optimizer_run_id': active_run.id if active_run else None},
        ).data,
        'eligible_physicians': eligible_physicians,
    }


@api_view(['GET', 'POST', 'PATCH'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_shift_assignments(request, block_id, shift_instance_id):
    block = get_object_or_404(ScheduleBlock.objects.select_related('domain'), id=block_id)
    shift_instance = get_object_or_404(
        ScheduleShiftInstance.objects.select_related(
            'facility',
            'schedule_version__domain',
        ),
        id=shift_instance_id,
        schedule_block=block,
    )
    if not _can_manage_build_workspace(request.user, shift_instance.schedule_version.domain):
        return _build_workspace_forbidden_response()

    if request.method == 'GET':
        return Response(_assignment_context_payload(shift_instance))

    if (
        block.build_status != ScheduleBlock.BuildStatus.BUILD
        or shift_instance.schedule_version.status != ScheduleVersion.Status.BUILD
    ):
        return Response(
            {'detail': 'Physicians can only be assigned in a BUILD Schedule Version.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    viewed_run, run_error = _requested_editable_run(request, shift_instance.schedule_version)
    if run_error:
        return run_error

    if request.method == 'PATCH' and request.data.get('physician_id') is None:
        with transaction.atomic():
            locked_instance = ScheduleShiftInstance.objects.select_for_update().get(id=shift_instance.id)
            active_run = _active_optimizer_run(locked_instance.schedule_version)
            locked_instance.assignments.filter(visible_assignment_filter(active_run)).delete()
            locked_instance.is_locked_open = bool(request.data.get('is_locked_open', False))
            locked_instance.save(update_fields=['is_locked_open', 'updated_at'])
            _set_active_run_locked_open(locked_instance, locked_instance.is_locked_open)
            _sync_shift_instance_status(locked_instance)
            _mark_schedule_score_stale(locked_instance.schedule_version, viewed_run)
        return Response(_assignment_context_payload(locked_instance))

    try:
        physician_id = int(request.data.get('physician_id'))
    except (TypeError, ValueError):
        return Response(
            {'physician_id': 'physician_id is required and must be a valid integer.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    physician = get_object_or_404(
        Physician.objects.select_related('user'),
        id=physician_id,
    )
    if not physician.active:
        return Response(
            {'physician_id': 'Physician is inactive.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    eligibility = _physician_assignment_eligibility(physician, shift_instance)
    if not eligibility['can_assign']:
        return Response(
            {
                'physician_id': eligibility['ineligibility_reason']
                or 'Physician is not eligible for this shift instance.'
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    with transaction.atomic():
        locked_instance = ScheduleShiftInstance.objects.select_for_update().get(
            id=shift_instance.id
        )
        active_run = _active_optimizer_run(locked_instance.schedule_version)
        if locked_instance.assignments.filter(
            visible_assignment_filter(active_run),
            shift_instance=locked_instance,
            physician=physician,
        ).exists():
            return Response(
                {'physician_id': 'Physician is already assigned to this shift instance.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if locked_instance.assignments.filter(visible_assignment_filter(active_run)).count() >= locked_instance.required_staffing:
            return Response(
                {'detail': 'This shift instance is already fully staffed.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        overlapping_assignment = _overlapping_assignment_for_physician(
            physician,
            locked_instance,
        )
        if overlapping_assignment:
            return Response(
                {'physician_id': _overlapping_assignment_message(
                    physician,
                    overlapping_assignment,
                )},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ScheduleShiftAssignment.objects.create(
            shift_instance=locked_instance,
            physician=physician,
            created_by=request.user,
            assignment_source=ScheduleShiftAssignment.AssignmentSource.MANUAL,
            optimizer_run=active_run,
            is_locked=bool(request.data.get('is_locked', False)),
        )
        if locked_instance.is_locked_open:
            locked_instance.is_locked_open = False
            locked_instance.save(update_fields=['is_locked_open', 'updated_at'])
            _set_active_run_locked_open(locked_instance, False)
        _sync_shift_instance_status(locked_instance)
        _mark_schedule_score_stale(locked_instance.schedule_version, viewed_run)

    return Response(
        _assignment_context_payload(locked_instance),
        status=status.HTTP_201_CREATED,
    )


@api_view(['PATCH', 'DELETE'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_shift_assignment_detail(request, block_id, shift_instance_id, assignment_id):
    block = get_object_or_404(ScheduleBlock.objects.select_related('domain'), id=block_id)
    shift_instance = get_object_or_404(
        ScheduleShiftInstance.objects.select_related('schedule_version__domain'),
        id=shift_instance_id,
        schedule_block=block,
    )
    if not _can_manage_build_workspace(request.user, shift_instance.schedule_version.domain):
        return _build_workspace_forbidden_response()
    if (
        block.build_status != ScheduleBlock.BuildStatus.BUILD
        or shift_instance.schedule_version.status != ScheduleVersion.Status.BUILD
    ):
        return Response(
            {'detail': 'Physicians can only be removed in a BUILD Schedule Version.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    viewed_run, run_error = _requested_editable_run(request, shift_instance.schedule_version)
    if run_error:
        return run_error

    assignment = get_object_or_404(
        ScheduleShiftAssignment.objects.filter(visible_assignment_filter(viewed_run)),
        id=assignment_id,
        shift_instance=shift_instance,
    )
    if request.method == 'PATCH':
        try:
            physician_id = int(request.data.get('physician_id'))
        except (TypeError, ValueError):
            return Response(
                {'physician_id': 'physician_id is required and must be a valid integer.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        physician = get_object_or_404(Physician.objects.select_related('user'), id=physician_id)
        if not physician.active:
            return Response(
                {'physician_id': 'Physician is inactive.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        eligibility = _physician_assignment_eligibility(physician, shift_instance)
        if not eligibility['can_assign']:
            return Response(
                {'physician_id': eligibility['ineligibility_reason'] or 'Physician is not eligible for this shift instance.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        with transaction.atomic():
            locked_assignment = ScheduleShiftAssignment.objects.select_for_update().get(id=assignment.id)
            active_run = _active_optimizer_run(shift_instance.schedule_version)
            duplicate = ScheduleShiftAssignment.objects.filter(
                visible_assignment_filter(active_run),
                shift_instance=shift_instance,
                physician=physician,
            ).exclude(id=locked_assignment.id).exists()
            if duplicate:
                return Response(
                    {'physician_id': 'Physician is already assigned to this shift instance.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            overlapping_assignment = _overlapping_assignment_for_physician(
                physician,
                shift_instance,
                exclude_assignment_id=locked_assignment.id,
            )
            if overlapping_assignment:
                return Response(
                    {'physician_id': _overlapping_assignment_message(physician, overlapping_assignment)},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            locked_assignment.physician = physician
            locked_assignment.assignment_source = ScheduleShiftAssignment.AssignmentSource.MANUAL
            locked_assignment.optimizer_run = active_run
            locked_assignment.is_locked = bool(request.data.get('is_locked', False))
            locked_assignment.created_by = request.user
            locked_assignment.save()
            if shift_instance.is_locked_open:
                shift_instance.is_locked_open = False
                shift_instance.save(update_fields=['is_locked_open', 'updated_at'])
                _set_active_run_locked_open(shift_instance, False)
            _mark_schedule_score_stale(shift_instance.schedule_version, viewed_run)
        return Response(_assignment_context_payload(shift_instance))
    assignment.delete()
    _sync_shift_instance_status(shift_instance)
    _mark_schedule_score_stale(shift_instance.schedule_version, viewed_run)
    return Response(_assignment_context_payload(shift_instance))


@api_view(['POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_version_unlock_physician_assignments(request, version_id, physician_id):
    version = get_object_or_404(
        ScheduleVersion.objects.select_related('schedule_block', 'domain'),
        id=version_id,
    )
    if not _can_manage_build_workspace(request.user, version.domain):
        return _build_workspace_forbidden_response()
    if (
        version.schedule_block.build_status != ScheduleBlock.BuildStatus.BUILD
        or version.status != ScheduleVersion.Status.BUILD
    ):
        return Response(
            {'detail': 'Assignments can only be unlocked in a BUILD Schedule Version.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    viewed_run, run_error = _requested_editable_run(request, version)
    if run_error:
        return run_error

    physician = get_object_or_404(
        Physician.objects.select_related('user'),
        id=physician_id,
    )
    with transaction.atomic():
        locked_assignments = ScheduleShiftAssignment.objects.select_for_update().filter(
            visible_assignment_filter(viewed_run),
            shift_instance__schedule_version=version,
            physician=physician,
            is_locked=True,
        )
        unlocked_count = locked_assignments.update(
            is_locked=False,
            updated_at=timezone.now(),
        )
        if unlocked_count:
            _mark_schedule_score_stale(version, viewed_run)

    return Response({
        'detail': (
            f'Unlocked {unlocked_count} locked shift'
            f'{"" if unlocked_count == 1 else "s"} for {_physician_display_name(physician)}.'
        ),
        'physician_id': physician.id,
        'physician_name': _physician_display_name(physician),
        'unlocked_count': unlocked_count,
        'optimizer_run_id': viewed_run.id if viewed_run else None,
    })


@api_view(['POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_block_generate_shift_instances(request, block_id):
    block = get_object_or_404(ScheduleBlock.objects.select_related('domain'), id=block_id)
    if not _can_manage_build_workspace(request.user, block.domain):
        return _build_workspace_forbidden_response()
    if block.build_status not in {
        ScheduleBlock.BuildStatus.PRE_BUILD,
        ScheduleBlock.BuildStatus.BUILD,
    }:
        return Response(
            {'detail': 'Shift instances can only be generated for PRE_BUILD or BUILD Schedule Blocks.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        domain_id = int(request.data.get('domain_id', block.domain_id))
    except (TypeError, ValueError):
        return Response(
            {'domain_id': 'domain_id is required and must be a valid integer.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if domain_id != block.domain_id:
        return Response(
            {'domain_id': 'This Schedule Block belongs to a different Domain.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    domain = get_object_or_404(Domain, id=block.domain_id, active=True)

    with transaction.atomic():
        version = (
            ScheduleVersion.objects.select_for_update()
            .filter(
                schedule_block=block,
                domain=domain,
                status=ScheduleVersion.Status.BUILD,
            )
            .order_by('-version_number')
            .first()
        )
        if version is None:
            latest_version_number = (
                ScheduleVersion.objects.filter(schedule_block=block, domain=domain)
                .order_by('-version_number')
                .values_list('version_number', flat=True)
                .first()
                or 0
            )
            version = ScheduleVersion.objects.create(
                schedule_block=block,
                domain=domain,
                version_number=latest_version_number + 1,
                name=f'Build {latest_version_number + 1}',
                status=ScheduleVersion.Status.BUILD,
            )

        templates = list(
            ShiftTemplate.objects.filter(active=True, facility__active=True, domain=domain)
            .select_related('facility')
            .order_by(*SHIFT_TEMPLATE_DISPLAY_ORDER)
        )
        template_fingerprint = _shift_template_fingerprint(block, templates)
        if version.shift_template_fingerprint == template_fingerprint:
            total_count = ScheduleShiftInstance.objects.filter(schedule_version=version).count()
            return Response({
                'message': 'Schedule shifts are already up to date.',
                'created_count': 0,
                'updated_count': 0,
                'total_count': total_count,
                'schedule_block': ScheduleBlockSerializer(block).data,
                'schedule_version': ScheduleVersionWorkspaceSerializer(version).data,
            })
        created_count = 0
        updated_count = 0
        current_date = block.start_date
        while current_date <= block.end_date:
            day_name = current_date.strftime('%A')
            for template in templates:
                if day_name not in (template.active_days_of_week or []):
                    continue

                timezone_info = _facility_timezone(template.facility)
                end_date = current_date
                if template.end_time <= template.start_time:
                    end_date = current_date + timedelta(days=1)

                shift_instance, created = ScheduleShiftInstance.objects.get_or_create(
                    schedule_version=version,
                    date=current_date,
                    shift_template=template,
                    defaults={
                        'schedule_block': block,
                        'facility': template.facility,
                        'start_datetime': datetime.combine(
                            current_date,
                            template.start_time,
                            tzinfo=timezone_info,
                        ),
                        'end_datetime': datetime.combine(
                            end_date,
                            template.end_time,
                            tzinfo=timezone_info,
                        ),
                        'required_staffing': template.default_staffing_count,
                        'status': ScheduleShiftInstance.Status.OPEN,
                    },
                )
                if created:
                    created_count += 1
                else:
                    expected_values = {
                        'facility': template.facility,
                        'start_datetime': datetime.combine(
                            current_date, template.start_time, tzinfo=timezone_info,
                        ),
                        'end_datetime': datetime.combine(
                            end_date, template.end_time, tzinfo=timezone_info,
                        ),
                        'required_staffing': template.default_staffing_count,
                    }
                    updated_fields = []
                    for field, expected_value in expected_values.items():
                        if getattr(shift_instance, field) != expected_value:
                            setattr(shift_instance, field, expected_value)
                            updated_fields.append(field)
                    if updated_fields:
                        shift_instance.save(update_fields=[*updated_fields, 'updated_at'])
                        updated_count += 1
            current_date += timedelta(days=1)

        version.shift_template_fingerprint = template_fingerprint
        version_update_fields = ['shift_template_fingerprint', 'updated_at']
        if created_count or updated_count:
            version.score_is_stale = True
            version_update_fields.append('score_is_stale')
            version.optimizer_runs.update(score_is_stale=True)
        version.save(update_fields=version_update_fields)

        if block.build_status == ScheduleBlock.BuildStatus.PRE_BUILD:
            block.build_status = ScheduleBlock.BuildStatus.BUILD
            block.save(update_fields=['build_status', 'updated_at'])

    total_count = ScheduleShiftInstance.objects.filter(schedule_version=version).count()
    return Response(
        {
            'message': (
                f'Added {created_count} and updated {updated_count} schedule shifts.'
                if created_count or updated_count
                else 'Schedule shifts are already up to date.'
            ),
            'created_count': created_count,
            'updated_count': updated_count,
            'total_count': total_count,
            'schedule_block': ScheduleBlockSerializer(block).data,
            'schedule_version': ScheduleVersionWorkspaceSerializer(version).data,
        }
    )


@api_view(['GET', 'POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_blocks_list_create(request):
    if request.method == 'GET':
        full_access_domain_ids = (
            permitted_domain_ids(request.user, 'manage_build_workspace')
            | permitted_domain_ids(request.user, 'administer_requests')
        )
        regular_block_ids = _request_blocks_for_regular_user(request.user).values_list('id', flat=True)
        blocks = ScheduleBlock.objects.filter(
            Q(domain_id__in=full_access_domain_ids) | Q(id__in=regular_block_ids),
        ).distinct()
        blocks = blocks.select_related('domain__region')
        domain_id = request.query_params.get('domain')
        region_id = request.query_params.get('region')
        if domain_id:
            blocks = blocks.filter(domain_id=domain_id)
        elif region_id:
            blocks = blocks.filter(domain__region_id=region_id)
        blocks = list(blocks)
        payload = [_serialize_schedule_block_for_user(request.user, block) for block in blocks]
        physician = _resolve_self_physician(request.user)
        requests_by_block = {}
        if physician is not None:
            own_requests = (
                ScheduleRequest.objects.filter(
                    schedule_block__in=blocks,
                    physician=physician,
                    request_scope=ScheduleRequest.RequestScope.USER,
                )
                .select_related('physician__user')
                .prefetch_related('shift_templates__facility')
                .order_by('date')
            )
            for schedule_request in own_requests:
                requests_by_block.setdefault(schedule_request.schedule_block_id, []).append(schedule_request)
        for block_payload in payload:
            block_payload['my_requests'] = ScheduleRequestSerializer(
                requests_by_block.get(block_payload['id'], []),
                many=True,
            ).data
        return Response(payload)

    serializer = ScheduleBlockSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    start_date = serializer.validated_data['start_date']
    end_date = serializer.validated_data['end_date']
    domain = serializer.validated_data['domain']
    if not _can_manage_build_workspace(request.user, domain):
        return _build_workspace_forbidden_response()
    acknowledged_overlap = bool(request.data.get('acknowledge_overlap', False))

    if _has_published_overlap(domain, start_date, end_date) and not acknowledged_overlap:
        return Response(
            {
                'warning': (
                    'A published Schedule Block already exists for one or more dates in this period. '
                    'If this Schedule Block is later published it will replace the existing Live '
                    'Schedule for those dates.'
                ),
                'requires_acknowledgement': True,
            },
            status=status.HTTP_409_CONFLICT,
        )

    block = serializer.save(build_status=ScheduleBlock.BuildStatus.PRE_BUILD)
    return Response(ScheduleBlockSerializer(block).data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PATCH', 'DELETE'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_block_detail(request, block_id):
    block = get_object_or_404(ScheduleBlock.objects.select_related('domain__region'), id=block_id)

    if request.method == 'GET':
        if not _can_access_schedule_block(request.user, block):
            return _build_workspace_forbidden_response()
        return Response(_serialize_schedule_block_for_user(request.user, block))

    if not _can_manage_build_workspace(request.user, block.domain):
        return _build_workspace_forbidden_response()

    if request.method == 'PATCH':
        partial = True
        serializer = ScheduleBlockSerializer(block, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    if block.published_at is not None:
        return Response(
            {'detail': 'Unpublish this Schedule Block before deleting it.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    with transaction.atomic():
        ShiftTrade.objects.filter(
            Q(offered_assignment__shift_instance__schedule_block=block)
            | Q(requested_assignment__shift_instance__schedule_block=block)
        ).delete()
        ScheduleShiftAssignment.objects.filter(
            shift_instance__schedule_block=block,
        ).delete()
        block.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_block_enter_preview(request, block_id):
    block = get_object_or_404(
        ScheduleBlock.objects.select_related('domain', 'preview_optimizer_run'), id=block_id,
    )
    if not _can_manage_build_workspace(request.user, block.domain):
        return _build_workspace_forbidden_response()

    if block.build_status == ScheduleBlock.BuildStatus.ARCHIVE:
        return Response({'detail': 'Archived Schedule Blocks cannot enter preview.'}, status=status.HTTP_400_BAD_REQUEST)

    optimizer_run_id = request.data.get('optimizer_run_id')
    if optimizer_run_id in (None, ''):
        return Response(
            {'optimizer_run_id': 'Select the completed optimizer run to preview.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        optimizer_run_id = int(optimizer_run_id)
    except (TypeError, ValueError):
        return Response(
            {'optimizer_run_id': 'optimizer_run_id must be a valid optimizer run ID.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    preview_run = get_object_or_404(
        OptimizerRun,
        id=optimizer_run_id,
        schedule_version__schedule_block=block,
        schedule_version__domain=block.domain,
    )
    if preview_run.status != OptimizerRun.Status.COMPLETED:
        return Response(
            {'optimizer_run_id': 'Only a completed optimizer run can enter preview.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if block.build_status == ScheduleBlock.BuildStatus.PREVIEW:
        if block.preview_optimizer_run_id != preview_run.id:
            return Response(
                {'detail': 'Move the Schedule Block back to BUILD before previewing a different run.'},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(ScheduleBlockSerializer(block).data)

    # Request intake is out of scope; allow PRE_BUILD to progress into PREVIEW for lifecycle testing.
    if block.build_status in {ScheduleBlock.BuildStatus.PRE_BUILD, ScheduleBlock.BuildStatus.BUILD}:
        block.build_status = ScheduleBlock.BuildStatus.PREVIEW
        block.preview_optimizer_run = preview_run
        block.save(update_fields=['build_status', 'preview_optimizer_run', 'updated_at'])
        return Response(ScheduleBlockSerializer(block).data)

    return Response({'detail': 'Schedule Block cannot enter preview from its current state.'}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_block_move_back_to_build(request, block_id):
    block = get_object_or_404(ScheduleBlock.objects.select_related('domain'), id=block_id)
    if not _can_manage_build_workspace(request.user, block.domain):
        return _build_workspace_forbidden_response()
    if block.build_status != ScheduleBlock.BuildStatus.PREVIEW:
        return Response(
            {'detail': 'Only PREVIEW Schedule Blocks can move back to BUILD.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    block.build_status = ScheduleBlock.BuildStatus.BUILD
    block.preview_optimizer_run = None
    block.save(update_fields=['build_status', 'preview_optimizer_run', 'updated_at'])
    return Response(ScheduleBlockSerializer(block).data)


@api_view(['POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_block_publish(request, block_id):
    block = get_object_or_404(ScheduleBlock.objects.select_related('domain'), id=block_id)
    if not (
        _can_manage_build_workspace(request.user, block.domain)
        and has_permission(request.user, 'publish_schedule', domain=block.domain)
    ):
        return Response(
            {'detail': 'Publish schedule permission is required for this Domain.'},
            status=status.HTTP_403_FORBIDDEN,
        )

    if block.build_status != ScheduleBlock.BuildStatus.PREVIEW:
        return Response({'detail': 'Only PREVIEW Schedule Blocks can be published.'}, status=status.HTTP_400_BAD_REQUEST)

    optimizer_run_id = request.data.get('optimizer_run_id')
    if optimizer_run_id in (None, ''):
        return Response(
            {'optimizer_run_id': 'Select the completed optimizer run to publish.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        optimizer_run_id = int(optimizer_run_id)
    except (TypeError, ValueError):
        return Response(
            {'optimizer_run_id': 'optimizer_run_id must be a valid optimizer run ID.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    published_run = get_object_or_404(
        OptimizerRun.objects.select_related('schedule_version'),
        id=optimizer_run_id,
        schedule_version__schedule_block=block,
        schedule_version__domain=block.domain,
    )
    if published_run.status != OptimizerRun.Status.COMPLETED:
        return Response(
            {'optimizer_run_id': 'Only a completed optimizer run can be published.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if block.preview_optimizer_run_id != published_run.id:
        return Response(
            {'optimizer_run_id': 'Only the optimizer run currently in Preview can be published.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    acknowledged_overlap = bool(request.data.get('acknowledge_overlap', False))
    if _has_published_overlap(
        block.domain,
        block.start_date,
        block.end_date,
        exclude_id=block.id,
    ) and not acknowledged_overlap:
        return Response(
            {
                'warning': (
                    'You are about to replace an existing Live Schedule for one or more dates.\n\n'
                    'The previous published Schedule Block will remain archived as the historical '
                    'schedule of record.\n\n'
                    'Continue?'
                ),
                'requires_acknowledgement': True,
            },
            status=status.HTTP_409_CONFLICT,
        )

    version = published_run.schedule_version
    report = build_violation_report(version, optimizer_run=published_run)
    frozen_report = json.loads(json.dumps(report, cls=DjangoJSONEncoder))

    with transaction.atomic():
        block = ScheduleBlock.objects.select_for_update().get(id=block.id)
        block.published_at = timezone.now()
        block.build_status = ScheduleBlock.BuildStatus.ARCHIVE
        block.preview_optimizer_run = None
        block.save(update_fields=[
            'published_at', 'build_status', 'preview_optimizer_run', 'updated_at',
        ])
        ScheduleVersion.objects.filter(
            schedule_block=block,
            domain=block.domain,
        ).exclude(id=version.id).update(
            published_optimizer_run=None,
            published_violation_report={},
        )
        ScheduleVersion.objects.filter(id=version.id).update(
            published_optimizer_run=published_run,
            published_violation_report=frozen_report,
            score_is_stale=False,
        )
        OptimizerRun.objects.filter(id=published_run.id).update(score_is_stale=False)
    return Response(ScheduleBlockSerializer(block).data)


@api_view(['POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_block_unpublish(request, block_id):
    block = get_object_or_404(ScheduleBlock.objects.select_related('domain'), id=block_id)
    if not (
        _can_manage_build_workspace(request.user, block.domain)
        and has_permission(request.user, 'unpublish_schedule', domain=block.domain)
    ):
        return Response(
            {'detail': 'Unpublish schedule permission is required for this Domain.'},
            status=status.HTTP_403_FORBIDDEN,
        )
    if block.build_status != ScheduleBlock.BuildStatus.ARCHIVE or block.published_at is None:
        return Response(
            {'detail': 'Only a published Schedule Block can be returned to BUILD.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    with transaction.atomic():
        block.published_at = None
        block.build_status = ScheduleBlock.BuildStatus.BUILD
        block.save(update_fields=['published_at', 'build_status', 'updated_at'])
        versions = ScheduleVersion.objects.filter(schedule_block=block)
        version_ids = list(versions.values_list('id', flat=True))
        versions.update(
            published_optimizer_run=None,
            published_violation_report={},
            score_is_stale=True,
        )
        OptimizerRun.objects.filter(schedule_version_id__in=version_ids).update(
            score_is_stale=True,
        )
    return Response(ScheduleBlockSerializer(block).data)


@api_view(['GET', 'POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def contracts_list_create(request):
    if request.method == 'GET':
        allowed_domain_ids = permitted_domain_ids(request.user, 'manage_build_workspace')
        contracts = Contract.objects.select_related('domain__region__organization').prefetch_related(
            'facilities',
            'user_assignments__physician__user',
            'shared_rule_links__shared_rule__shift_templates__facility',
        ).filter(domain_id__in=allowed_domain_ids)

        domain_id = request.query_params.get('domain')
        region_id = request.query_params.get('region')
        include_inactive = request.query_params.get('include_inactive') == 'true'
        search = (request.query_params.get('search') or '').strip()

        if domain_id:
            contracts = contracts.filter(domain_id=domain_id)
        elif region_id:
            contracts = contracts.filter(domain__region_id=region_id)

        if not include_inactive:
            contracts = contracts.filter(active=True)

        if search:
            contracts = contracts.filter(name__icontains=search)

        serializer = ContractSerializer(contracts, many=True)
        return Response(serializer.data)

    serializer = ContractSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    domain = serializer.validated_data['domain']
    if not _can_manage_build_workspace(request.user, domain):
        return _build_workspace_forbidden_response()
    contract = serializer.save()
    _mark_contract_domain_scores_stale(contract)
    return Response(ContractSerializer(contract).data, status=status.HTTP_201_CREATED)


def _shared_rule_queryset():
    return (
        SharedRule.objects.select_related('domain__region__organization')
        .prefetch_related(
            'shift_templates__facility',
            'contract_links__contract',
        )
    )


@api_view(['GET', 'POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def shared_rules_list_create(request):
    if request.method == 'GET':
        queryset = _shared_rule_queryset().filter(
            domain_id__in=permitted_domain_ids(request.user, 'manage_build_workspace'),
        )
        domain_id = request.query_params.get('domain')
        region_id = request.query_params.get('region')
        active_view = request.query_params.get('status', 'active')
        if domain_id:
            queryset = queryset.filter(domain_id=domain_id)
        elif region_id:
            queryset = queryset.filter(domain__region_id=region_id)
        queryset = queryset.filter(active=active_view != 'inactive')
        return Response(SharedRuleSerializer(queryset, many=True).data)

    serializer = SharedRuleSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    domain = serializer.validated_data['domain']
    if not _can_manage_build_workspace(request.user, domain):
        return _build_workspace_forbidden_response()
    with transaction.atomic():
        shared_rule = serializer.save()
        first_link = shared_rule.contract_links.select_related('contract').first()
        if first_link:
            _mark_contract_domain_scores_stale(first_link.contract)
    shared_rule = _shared_rule_queryset().get(id=shared_rule.id)
    return Response(
        SharedRuleSerializer(shared_rule).data,
        status=status.HTTP_201_CREATED,
    )


@api_view(['GET', 'PUT', 'PATCH', 'DELETE'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def shared_rule_detail(request, shared_rule_id):
    shared_rule = get_object_or_404(
        _shared_rule_queryset(), id=shared_rule_id,
    )
    if not _can_manage_build_workspace(request.user, shared_rule.domain):
        return _build_workspace_forbidden_response()
    if request.method == 'GET':
        return Response(SharedRuleSerializer(shared_rule).data)
    if request.method == 'DELETE':
        with transaction.atomic():
            contract_ids = list(
                shared_rule.contract_links.values_list('contract_id', flat=True)
            )
            first_contract = Contract.objects.filter(
                id__in=contract_ids,
            ).first()
            remove_shared_rule_from_contracts(shared_rule.id, contract_ids)
            shared_rule.delete()
            if first_contract:
                _mark_contract_domain_scores_stale(first_contract)
        return Response(status=status.HTTP_204_NO_CONTENT)

    serializer = SharedRuleSerializer(
        shared_rule,
        data=request.data,
        partial=request.method == 'PATCH',
    )
    serializer.is_valid(raise_exception=True)
    with transaction.atomic():
        shared_rule = serializer.save()
        first_link = shared_rule.contract_links.select_related('contract').first()
        if first_link:
            _mark_contract_domain_scores_stale(first_link.contract)
    shared_rule = _shared_rule_queryset().get(id=shared_rule.id)
    return Response(SharedRuleSerializer(shared_rule).data)


@api_view(['POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def shared_rule_duplicate(request, shared_rule_id):
    source = get_object_or_404(
        _shared_rule_queryset(), id=shared_rule_id,
    )
    target_domain_id = request.data.get('domain')
    target_domain = get_object_or_404(
        Domain.objects.select_related('region'), id=target_domain_id, active=True,
    )
    if not (
        _can_manage_build_workspace(request.user, source.domain)
        and _can_manage_build_workspace(request.user, target_domain)
    ):
        return _build_workspace_forbidden_response()
    base_name = f'{source.name} (Copy)'
    next_name = base_name
    suffix = 2
    while SharedRule.objects.filter(domain=target_domain, name=next_name).exists():
        next_name = f'{base_name} {suffix}'
        suffix += 1

    reference_contract_settings = [{
        'contract_name': link.contract.name,
        'enabled': link.enabled,
        'min_value': link.min_value,
        'max_value': link.max_value,
        'min_penalty_weight': link.min_penalty_weight,
        'max_penalty_weight': link.max_penalty_weight,
        'spread_violations': link.spread_violations,
    } for link in source.contract_links.select_related('contract').all()]
    reference_shift_templates = [
        template.generated_name()
        for template in source.shift_templates.select_related('facility').all()
    ]
    duplicate = SharedRule.objects.create(
        domain=target_domain,
        name=next_name,
        active=True,
        period_type=source.period_type,
        units=source.units,
        reference_contract_settings=reference_contract_settings,
        reference_shift_templates=reference_shift_templates,
    )
    return Response(
        SharedRuleSerializer(_shared_rule_queryset().get(id=duplicate.id)).data,
        status=status.HTTP_201_CREATED,
    )


@api_view(['GET', 'PUT', 'PATCH', 'DELETE'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def contract_detail(request, contract_id):
    contract = get_object_or_404(
        Contract.objects.select_related('domain').prefetch_related(
            'facilities',
            'user_assignments__physician__user',
            'shared_rule_links__shared_rule__shift_templates__facility',
        ),
        id=contract_id,
    )
    if not _can_manage_build_workspace(request.user, contract.domain):
        return _build_workspace_forbidden_response()

    if request.method == 'DELETE':
        with transaction.atomic():
            contract = Contract.objects.select_for_update().get(id=contract.id)
            if contract.user_assignments.exists():
                return Response(
                    {'detail': 'Remove all assigned users before deleting this Contract.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            contract.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    if request.method == 'GET':
        serializer = ContractSerializer(contract)
        return Response(serializer.data)

    partial = request.method == 'PATCH'
    serializer = ContractSerializer(contract, data=request.data, partial=partial)
    serializer.is_valid(raise_exception=True)
    serializer.save()
    contract.refresh_from_db()
    _mark_contract_domain_scores_stale(contract)
    return Response(ContractSerializer(contract).data)


def _copy_json_dict(source_value):
    if isinstance(source_value, dict):
        return copy.deepcopy(source_value)
    return {}


def _build_duplicate_contract_name(source_contract, target_domain=None):
    target_domain = target_domain or source_contract.domain
    base_name = f'{source_contract.name} (Copy)'
    next_name = base_name
    suffix = 2

    while Contract.objects.filter(domain=target_domain, name=next_name).exists():
        next_name = f'{base_name} {suffix}'
        suffix += 1

    return next_name


@api_view(['POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def contract_duplicate(request, contract_id):
    source_contract = get_object_or_404(
        Contract.objects.select_related('domain__region').prefetch_related(
            'facilities', 'user_assignments',
            'shared_rule_links__shared_rule',
        ),
        id=contract_id,
    )

    target_domain_id = request.data.get('domain') or source_contract.domain_id
    target_domain = get_object_or_404(
        Domain.objects.select_related('region'), id=target_domain_id, active=True,
    )
    if not (
        _can_manage_build_workspace(request.user, source_contract.domain)
        and _can_manage_build_workspace(request.user, target_domain)
    ):
        return _build_workspace_forbidden_response()
    cross_region_copy = target_domain.region_id != source_contract.domain.region_id
    source_facilities = list(source_contract.facilities.all())
    if not cross_region_copy:
        target_facilities = source_facilities
    else:
        target_facilities = []

    shift_settings = _copy_json_dict(source_contract.shift_settings)
    if cross_region_copy:
        shift_settings['rules'] = []
    source_template_ids = {
        int(template_id)
        for rule in shift_settings.get('rules', [])
        if isinstance(rule, dict)
        for template_id in (rule.get('shift_template_ids') or [])
    }
    template_id_map = {}
    if source_template_ids and target_domain.id != source_contract.domain_id:
        source_templates = ShiftTemplate.objects.filter(
            id__in=source_template_ids,
        ).select_related('facility')
        target_templates = ShiftTemplate.objects.filter(
            domain=target_domain,
        ).select_related('facility')
        targets_by_signature = {
            (
                template.facility.short_name.strip().casefold(),
                template.start_time,
                template.end_time,
            ): template.id
            for template in target_templates
        }
        missing_templates = []
        for template in source_templates:
            signature = (
                template.facility.short_name.strip().casefold(),
                template.start_time,
                template.end_time,
            )
            target_template_id = targets_by_signature.get(signature)
            if target_template_id is None:
                missing_templates.append(template.generated_name())
            else:
                template_id_map[template.id] = target_template_id
        if missing_templates:
            return Response({
                'detail': (
                    'Create matching destination Shift Templates before copying this Contract: '
                    f'{", ".join(sorted(missing_templates))}.'
                ),
            }, status=status.HTTP_400_BAD_REQUEST)
        for rule in shift_settings.get('rules', []):
            if isinstance(rule, dict) and rule.get('shift_template_ids'):
                rule['shift_template_ids'] = [
                    template_id_map[int(template_id)]
                    for template_id in rule['shift_template_ids']
                ]

    source_links = (
        [] if cross_region_copy
        else list(source_contract.shared_rule_links.select_related('shared_rule'))
    )
    target_shared_rules_by_name = {
        rule.name.strip().casefold(): rule
        for rule in SharedRule.objects.filter(domain=target_domain, active=True)
    }
    missing_shared_rules = [
        link.shared_rule.name for link in source_links
        if link.shared_rule.name.strip().casefold() not in target_shared_rules_by_name
    ]
    if target_domain.id != source_contract.domain_id and missing_shared_rules:
        return Response({
            'detail': (
                'Create matching destination Shared Rules before copying this Contract: '
                f'{", ".join(sorted(missing_shared_rules))}.'
            ),
        }, status=status.HTTP_400_BAD_REQUEST)
    shared_rule_id_map = {
        link.shared_rule_id: (
            link.shared_rule_id if target_domain.id == source_contract.domain_id
            else target_shared_rules_by_name[link.shared_rule.name.strip().casefold()].id
        )
        for link in source_links
    }
    for rule in shift_settings.get('rules', []):
        if isinstance(rule, dict) and rule.get('shared_rule_id'):
            rule['shared_rule_id'] = shared_rule_id_map[int(rule['shared_rule_id'])]

    with transaction.atomic():
        duplicate = Contract.objects.create(
            domain=target_domain,
            name=_build_duplicate_contract_name(source_contract, target_domain),
            active=True,
            manual_assignment_only=source_contract.manual_assignment_only,
            workload_settings=_copy_json_dict(source_contract.workload_settings),
            shift_settings=shift_settings,
            night_settings=_copy_json_dict(source_contract.night_settings),
            weekend_settings=_copy_json_dict(source_contract.weekend_settings),
            request_settings=_copy_json_dict(source_contract.request_settings),
        )
        duplicate.facilities.set(target_facilities)
        SharedRuleContract.objects.bulk_create([
            SharedRuleContract(
                shared_rule_id=shared_rule_id_map[link.shared_rule_id],
                contract=duplicate,
                enabled=link.enabled,
                min_value=link.min_value,
                max_value=link.max_value,
                min_penalty_weight=link.min_penalty_weight,
                max_penalty_weight=link.max_penalty_weight,
                spread_violations=link.spread_violations,
            )
            for link in source_contract.shared_rule_links.select_related(
                'shared_rule',
            )
        ])
        for shared_rule in duplicate.shared_rules.all():
            sync_shared_rule_contract_settings(shared_rule)

    serializer = ContractSerializer(duplicate)
    return Response(serializer.data, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def contract_deactivate(request, contract_id):
    contract = get_object_or_404(Contract.objects.select_related('domain'), id=contract_id)
    if not _can_manage_build_workspace(request.user, contract.domain):
        return _build_workspace_forbidden_response()
    contract.active = False
    contract.save(update_fields=['active', 'updated_at'])
    _mark_contract_domain_scores_stale(contract)
    return Response(ContractSerializer(contract).data)


@api_view(['POST'])
@authentication_classes([CsrfProtectedSessionAuthentication])
@permission_classes([IsAuthenticated])
def contract_reactivate(request, contract_id):
    contract = get_object_or_404(Contract.objects.select_related('domain'), id=contract_id)
    if not _can_manage_build_workspace(request.user, contract.domain):
        return _build_workspace_forbidden_response()
    contract.active = True
    contract.save(update_fields=['active', 'updated_at'])
    _mark_contract_domain_scores_stale(contract)
    return Response(ContractSerializer(contract).data)
