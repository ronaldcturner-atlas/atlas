from datetime import date, time

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.domains.models import (
    Domain,
    DomainMembership,
    Organization,
    OrganizationMembership,
    Region,
    RoleTemplate,
)
from apps.domains.permissions import permitted_domain_ids
from apps.facilities.models import Facility

from .models import Contract, Physician, ScheduleBlock, ScheduleVersion, SharedRule, ShiftTemplate


class DomainAccessBoundaryTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user('domain-a-scheduler', password='x')
        self.org_admin = user_model.objects.create_user('organization-admin', password='x')
        self.organization = Organization.objects.create(name='Boundary Organization')
        self.region = Region.objects.create(
            organization=self.organization,
            name='Boundary Region',
        )
        self.domain_a = Domain.objects.create(region=self.region, name='Domain A')
        self.domain_b = Domain.objects.create(region=self.region, name='Domain B')
        OrganizationMembership.objects.create(
            organization=self.organization,
            user=self.user,
        )
        OrganizationMembership.objects.create(
            organization=self.organization,
            user=self.org_admin,
            is_org_admin=True,
        )
        role = RoleTemplate.objects.create(
            region=self.region,
            name='Domain A Scheduler',
            permissions=['manage_build_workspace', 'manage_shift_templates'],
        )
        DomainMembership.objects.create(
            domain=self.domain_a,
            user=self.user,
            role=DomainMembership.Role.SCHEDULER,
            role_template=role,
            clinically_active=False,
        )
        self.contract_a = Contract.objects.create(domain=self.domain_a, name='A Contract')
        self.contract_b = Contract.objects.create(domain=self.domain_b, name='B Contract')
        self.rule_a = SharedRule.objects.create(domain=self.domain_a, name='A Rule')
        self.rule_b = SharedRule.objects.create(domain=self.domain_b, name='B Rule')
        self.local_facility = Facility.objects.create(
            region=self.region,
            name='Boundary Facility',
            short_name='Boundary',
        )
        self.contract_a.facilities.add(self.local_facility)
        self.contract_b.facilities.add(self.local_facility)
        self.template_a = ShiftTemplate.objects.create(
            domain=self.domain_a,
            facility=self.local_facility,
            start_time=time(7, 0),
            end_time=time(19, 0),
            active_days_of_week=['Monday'],
        )
        self.template_b = ShiftTemplate.objects.create(
            domain=self.domain_b,
            facility=self.local_facility,
            start_time=time(19, 0),
            end_time=time(7, 0),
            active_days_of_week=['Monday'],
        )
        now = timezone.now()
        self.block_a = ScheduleBlock.objects.create(
            domain=self.domain_a,
            start_date=date(2027, 1, 1),
            end_date=date(2027, 1, 31),
            request_open_datetime=now,
            request_close_datetime=now,
        )
        self.block_b = ScheduleBlock.objects.create(
            domain=self.domain_b,
            start_date=date(2027, 2, 1),
            end_date=date(2027, 2, 28),
            request_open_datetime=now,
            request_close_datetime=now,
        )
        self.version_b = ScheduleVersion.objects.create(
            schedule_block=self.block_b,
            domain=self.domain_b,
            version_number=1,
            name='Build 1',
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_lists_only_return_managed_domain_records(self):
        contracts = self.client.get('/api/contracts/').json()
        rules = self.client.get('/api/shared-rules/').json()
        blocks = self.client.get('/api/schedule-blocks/').json()

        self.assertEqual({row['id'] for row in contracts}, {self.contract_a.id})
        self.assertEqual({row['id'] for row in rules}, {self.rule_a.id})
        self.assertEqual({row['id'] for row in blocks}, {self.block_a.id})

    def test_direct_cross_domain_configuration_access_is_denied(self):
        self.assertEqual(
            self.client.get(f'/api/contracts/{self.contract_b.id}/').status_code,
            403,
        )
        self.assertEqual(
            self.client.patch(
                f'/api/contracts/{self.contract_b.id}/',
                {'name': 'Changed'},
                format='json',
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(f'/api/shared-rules/{self.rule_b.id}/').status_code,
            403,
        )
        self.assertEqual(
            self.client.patch(
                f'/api/shared-rules/{self.rule_b.id}/',
                {'name': 'Changed Rule'},
                format='json',
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.delete(
                f'/api/shared-rules/{self.rule_b.id}/',
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                f'/api/shared-rules/{self.rule_a.id}/duplicate/',
                {'domain': self.domain_b.id},
                format='json',
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                f'/api/contracts/{self.contract_a.id}/duplicate/',
                {'domain': self.domain_b.id},
                format='json',
            ).status_code,
            403,
        )
        move_response = self.client.patch(
            f'/api/contracts/{self.contract_a.id}/',
            {'domain': self.domain_b.id},
            format='json',
        )
        self.assertEqual(move_response.status_code, 400)
        self.contract_a.refresh_from_db()
        self.assertEqual(self.contract_a.domain_id, self.domain_a.id)
        self.assertEqual(
            self.client.post(
                f'/api/contracts/{self.contract_b.id}/deactivate/',
                {},
                format='json',
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                f'/api/contracts/{self.contract_b.id}/reactivate/',
                {},
                format='json',
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.delete(f'/api/contracts/{self.contract_b.id}/').status_code,
            403,
        )

    def test_cross_domain_contract_and_shared_rule_creation_is_denied(self):
        contract_response = self.client.post(
            '/api/contracts/',
            {'domain': self.domain_b.id, 'name': 'Unauthorized Contract'},
            format='json',
        )
        rule_response = self.client.post(
            '/api/shared-rules/',
            {
                'domain': self.domain_b.id,
                'name': 'Unauthorized Rule',
                'active': True,
                'period_type': 'SCHEDULE_BLOCK',
                'units': 'SHIFTS',
                'shift_template_ids': [self.template_b.id],
                'contract_settings': [{
                    'contract_id': self.contract_b.id,
                    'min_value': 1,
                    'max_value': 2,
                    'min_penalty_weight': 100,
                    'max_penalty_weight': 100,
                    'spread_violations': False,
                }],
            },
            format='json',
        )

        self.assertEqual(contract_response.status_code, 403)
        self.assertEqual(rule_response.status_code, 403)

    def test_shared_rule_rejects_shift_from_another_domain(self):
        response = self.client.post(
            '/api/shared-rules/',
            {
                'domain': self.domain_a.id,
                'name': 'Mixed Domain Rule',
                'active': True,
                'period_type': 'SCHEDULE_BLOCK',
                'units': 'SHIFTS',
                'shift_template_ids': [self.template_b.id],
                'contract_settings': [{
                    'contract_id': self.contract_a.id,
                    'min_value': 1,
                    'max_value': 2,
                    'min_penalty_weight': 100,
                    'max_penalty_weight': 100,
                    'spread_violations': False,
                }],
            },
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('shift_template_ids', response.json())

    def test_direct_cross_domain_build_access_is_denied(self):
        self.assertEqual(
            self.client.get(f'/api/schedule-blocks/{self.block_b.id}/').status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                f'/api/schedule-blocks/{self.block_b.id}/build/versions/',
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                f'/api/schedule-versions/{self.version_b.id}/optimizer-runs/',
            ).status_code,
            403,
        )

    def test_shift_builder_only_exposes_and_changes_permitted_domain(self):
        domains_response = self.client.get(
            '/api/domains/?active=true&permission=manage_shift_templates',
        )
        templates_response = self.client.get('/api/shift-templates/')

        self.assertEqual(domains_response.status_code, 200)
        self.assertEqual(
            {row['id'] for row in domains_response.json()},
            {self.domain_a.id},
        )
        self.assertEqual(templates_response.status_code, 200)
        self.assertEqual(
            {row['id'] for row in templates_response.json()},
            {self.template_a.id},
        )
        self.assertEqual(
            self.client.get(f'/api/shift-templates/{self.template_b.id}/').status_code,
            403,
        )
        self.assertEqual(
            self.client.patch(
                f'/api/shift-templates/{self.template_b.id}/',
                {'active': False},
                format='json',
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                '/api/shift-templates/',
                {
                    'domain': self.domain_b.id,
                    'facility': self.local_facility.id,
                    'start_time': '08:00:00',
                    'end_time': '16:00:00',
                    'active_days_of_week': ['Tuesday'],
                    'weekend_days': [],
                    'night_shift': False,
                    'default_staffing_count': 1,
                    'active': True,
                },
                format='json',
            ).status_code,
            403,
        )

    def test_shift_template_facility_must_match_domain_region(self):
        other_region = Region.objects.create(
            organization=self.organization,
            name='Other Boundary Region',
        )
        foreign_facility = Facility.objects.create(
            region=other_region,
            name='Foreign Boundary Facility',
            short_name='Foreign',
        )

        response = self.client.post(
            '/api/shift-templates/',
            {
                'domain': self.domain_a.id,
                'facility': foreign_facility.id,
                'start_time': '08:00:00',
                'end_time': '16:00:00',
                'active_days_of_week': ['Tuesday'],
                'weekend_days': [],
                'night_shift': False,
                'default_staffing_count': 1,
                'active': True,
            },
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('facility', response.json())

    def test_domain_scheduler_cannot_manage_regional_facilities(self):
        self.assertEqual(
            self.client.post(
                '/api/facilities/',
                {
                    'region': self.region.id,
                    'name': 'Unauthorized Facility',
                    'short_name': 'Unauthorized',
                    'timezone': 'UTC',
                    'color': '#2563eb',
                },
                format='json',
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.patch(
                f'/api/facilities/{self.local_facility.id}/',
                {'name': 'Unauthorized Change'},
                format='json',
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                '/api/facilities/reorder/',
                {
                    'region': self.region.id,
                    'facility_ids': [self.local_facility.id],
                },
                format='json',
            ).status_code,
            403,
        )

    def test_publish_is_independent_from_build_workspace_permission(self):
        block_payload = self.client.get('/api/schedule-blocks/').json()[0]
        self.assertTrue(block_payload['can_manage_build_workspace'])
        self.assertFalse(block_payload['can_publish_schedule'])
        self.assertEqual(
            self.client.post(
                f'/api/schedule-blocks/{self.block_a.id}/publish/',
                {},
                format='json',
            ).status_code,
            403,
        )

    def test_publish_permission_without_build_workspace_permission_cannot_publish(self):
        publisher = get_user_model().objects.create_user('publisher-only', password='x')
        role = RoleTemplate.objects.create(
            region=self.region,
            name='Publisher Only',
            permissions=['publish_schedule'],
        )
        DomainMembership.objects.create(
            domain=self.domain_a,
            user=publisher,
            role=DomainMembership.Role.SCHEDULER,
            role_template=role,
            clinically_active=False,
        )
        self.client.force_authenticate(publisher)

        response = self.client.post(
            f'/api/schedule-blocks/{self.block_a.id}/publish/', {}, format='json',
        )

        self.assertEqual(response.status_code, 403)

    def test_preview_permission_only_exposes_preview_blocks_in_its_domain(self):
        viewer = get_user_model().objects.create_user('preview-only', password='x')
        role = RoleTemplate.objects.create(
            region=self.region,
            name='Preview Only',
            permissions=['view_preview'],
        )
        DomainMembership.objects.create(
            domain=self.domain_a,
            user=viewer,
            role=DomainMembership.Role.VIEW_ONLY,
            role_template=role,
            clinically_active=False,
        )
        self.client.force_authenticate(viewer)

        self.assertEqual(self.client.get('/api/schedule-blocks/').json(), [])
        self.block_a.build_status = ScheduleBlock.BuildStatus.PREVIEW
        self.block_a.save(update_fields=['build_status', 'updated_at'])

        blocks = self.client.get('/api/schedule-blocks/').json()
        context = self.client.get(f'/api/schedule-blocks/{self.block_a.id}/build/')

        self.assertEqual([row['id'] for row in blocks], [self.block_a.id])
        self.assertFalse(blocks[0]['can_manage_build_workspace'])
        self.assertTrue(blocks[0]['can_open_build_workspace'])
        self.assertEqual(context.status_code, 200)
        self.assertEqual(
            self.client.get(f'/api/schedule-blocks/{self.block_b.id}/build/').status_code,
            403,
        )

    def test_request_permissions_do_not_grant_build_workspace_access(self):
        requester = get_user_model().objects.create_user('requester-only', password='x')
        Physician.objects.create(user=requester, display_name='Requester Only')
        role = RoleTemplate.objects.create(
            region=self.region,
            name='Requester Only',
            permissions=['submit_own_requests'],
        )
        DomainMembership.objects.create(
            domain=self.domain_a,
            user=requester,
            role=DomainMembership.Role.STAFF_PHYSICIAN,
            role_template=role,
            clinically_active=True,
        )
        self.client.force_authenticate(requester)

        blocks = self.client.get('/api/schedule-blocks/').json()
        request_context = self.client.get(
            f'/api/schedule-blocks/{self.block_a.id}/requests/context/',
        )

        self.assertEqual([row['id'] for row in blocks], [self.block_a.id])
        self.assertTrue(blocks[0]['can_submit_own_requests'])
        self.assertFalse(blocks[0]['can_manage_build_workspace'])
        self.assertEqual(request_context.status_code, 200)
        self.assertEqual(
            self.client.get(f'/api/schedule-blocks/{self.block_a.id}/build/').status_code,
            403,
        )
        self.assertEqual(
            self.client.get(f'/api/schedule-blocks/{self.block_b.id}/requests/context/').status_code,
            403,
        )

    def test_domain_permission_union_returns_only_domains_with_any_requested_permission(self):
        response = self.client.get(
            '/api/domains/?permissions=manage_build_workspace,view_preview',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual({row['id'] for row in response.json()}, {self.domain_a.id})

    def test_org_admin_without_domain_memberships_has_all_org_domains(self):
        self.assertEqual(
            permitted_domain_ids(self.org_admin, 'manage_build_workspace'),
            {self.domain_a.id, self.domain_b.id},
        )
        self.client.force_authenticate(self.org_admin)
        response = self.client.get('/api/contracts/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {row['id'] for row in response.json()},
            {self.contract_a.id, self.contract_b.id},
        )

    def test_legacy_scheduler_group_cannot_bypass_domain_roles(self):
        legacy_user = get_user_model().objects.create_user(
            'legacy-global-scheduler', password='x',
        )
        scheduler_group, _ = Group.objects.get_or_create(name='Scheduler')
        legacy_user.groups.add(scheduler_group)
        OrganizationMembership.objects.create(
            organization=self.organization,
            user=legacy_user,
        )
        self.client.force_authenticate(legacy_user)

        self.assertEqual(
            self.client.get(f'/api/contracts/{self.contract_a.id}/').status_code,
            403,
        )
        self.assertEqual(self.client.get('/api/contracts/').json(), [])

    def test_facility_cannot_be_moved_to_another_region(self):
        membership = DomainMembership.objects.get(
            domain=self.domain_a,
            user=self.user,
        )
        membership.role_template.permissions = [
            *membership.role_template.permissions,
            'manage_regional_facilities',
        ]
        membership.role_template.save(update_fields=['permissions', 'updated_at'])
        other_region = Region.objects.create(
            organization=self.organization,
            name='Other Region',
        )

        response = self.client.patch(
            f'/api/facilities/{self.local_facility.id}/',
            {'region': other_region.id},
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.local_facility.refresh_from_db()
        self.assertEqual(self.local_facility.region_id, self.region.id)
