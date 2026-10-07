from datetime import date, datetime, time

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Physician
from apps.domains.models import (
    Domain,
    DomainMembership,
    Organization,
    OrganizationMembership,
    Region,
    RoleTemplate,
)
from apps.facilities.models import Facility

from .models import (
    OptimizerRun,
    ScheduleBlock,
    ScheduleShiftAssignment,
    ScheduleShiftInstance,
    ScheduleVersion,
    ShiftStatsGroup,
    ShiftTemplate,
)


class GroupStatsScopeTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.viewer = user_model.objects.create_user('stats-viewer', password='x')
        worker_user = user_model.objects.create_user('stats-worker', password='x')
        self.worker = Physician.objects.create(user=worker_user, display_name='Worker')
        organization = Organization.objects.create(name='Stats Organization')
        region = Region.objects.create(organization=organization, name='Region A')
        self.allowed_domain = Domain.objects.create(region=region, name='Allowed')
        self.denied_domain = Domain.objects.create(region=region, name='Denied')
        OrganizationMembership.objects.create(organization=organization, user=self.viewer)
        stats_role = RoleTemplate.objects.create(
            region=region,
            name='Stats Viewer',
            permissions=['view_domain_statistics'],
        )
        no_stats_role = RoleTemplate.objects.create(
            region=region,
            name='No Stats',
            permissions=['view_published_schedules'],
        )
        DomainMembership.objects.create(
            domain=self.allowed_domain,
            user=self.viewer,
            role=DomainMembership.Role.VIEW_ONLY,
            role_template=stats_role,
            clinically_active=False,
        )
        DomainMembership.objects.create(
            domain=self.denied_domain,
            user=self.viewer,
            role=DomainMembership.Role.VIEW_ONLY,
            role_template=no_stats_role,
            clinically_active=False,
        )
        self.allowed_template = self._publish_shift(self.allowed_domain, region, 'Allowed Hospital')
        self.denied_template = self._publish_shift(self.denied_domain, region, 'Denied Hospital')
        allowed_group = ShiftStatsGroup.objects.create(name='Allowed column')
        allowed_group.shift_templates.add(self.allowed_template)
        denied_group = ShiftStatsGroup.objects.create(name='Denied column')
        denied_group.shift_templates.add(self.denied_template)
        self.client = APIClient()
        self.client.force_authenticate(self.viewer)

    def _publish_shift(self, domain, region, facility_name):
        facility = Facility.objects.create(
            region=region,
            name=facility_name,
            short_name=facility_name.split()[0],
        )
        template = ShiftTemplate.objects.create(
            domain=domain,
            facility=facility,
            start_time=time(7),
            end_time=time(15),
            active_days_of_week=['Thursday'],
        )
        block = ScheduleBlock.objects.create(
            domain=domain,
            start_date=date(2026, 10, 1),
            end_date=date(2026, 10, 31),
            request_open_datetime=timezone.now(),
            request_close_datetime=timezone.now(),
            build_status=ScheduleBlock.BuildStatus.ARCHIVE,
            published_at=timezone.now(),
        )
        version = ScheduleVersion.objects.create(
            schedule_block=block,
            domain=domain,
            version_number=1,
            name='Published',
        )
        run = OptimizerRun.objects.create(
            schedule_version=version,
            run_number=1,
            status=OptimizerRun.Status.COMPLETED,
            is_active=True,
        )
        version.published_optimizer_run = run
        version.save(update_fields=['published_optimizer_run'])
        instance = ScheduleShiftInstance.objects.create(
            schedule_version=version,
            schedule_block=block,
            date=date(2026, 10, 1),
            shift_template=template,
            facility=facility,
            start_datetime=timezone.make_aware(datetime(2026, 10, 1, 7)),
            end_datetime=timezone.make_aware(datetime(2026, 10, 1, 15)),
            status=ScheduleShiftInstance.Status.ASSIGNED,
        )
        ScheduleShiftAssignment.objects.create(
            shift_instance=instance,
            physician=self.worker,
            assignment_source=ScheduleShiftAssignment.AssignmentSource.OPTIMIZER,
            optimizer_run=run,
        )
        return template

    def test_group_stats_only_returns_domains_allowed_by_role_permissions(self):
        response = self.client.get('/api/published-schedule/?purpose=stats')

        self.assertEqual(response.status_code, 200)
        self.assertEqual({row['domain'] for row in response.json()}, {self.allowed_domain.id})

    def test_stats_groups_only_return_columns_for_allowed_domains(self):
        response = self.client.get('/api/stats-groups/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual([row['name'] for row in response.json()], ['Allowed column'])
        self.assertEqual(response.json()[0]['domain_ids'], [self.allowed_domain.id])
