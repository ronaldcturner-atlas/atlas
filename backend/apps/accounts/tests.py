import json

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, TestCase
from django.utils import timezone

from apps.scheduling.models import (
    Contract,
    ContractUserAssignment,
    Domain,
    OptimizerControl,
    OptimizerRun,
    ScheduleBlock,
    ScheduleVersion,
)
from apps.domains.models import (
    DomainMembership,
    Organization,
    OrganizationMembership,
    Region,
    RoleTemplate,
)
from .models import Physician


class AccountsTests(TestCase):
    def test_physician_email_is_required_username_and_phone_is_optional(self):
        manager = get_user_model().objects.create_user(
            username='manager@example.com',
            email='manager@example.com',
            password='atlas',
        )
        organization = Organization.objects.create(name='Account Test Organization')
        OrganizationMembership.objects.create(
            organization=organization,
            user=manager,
            is_org_admin=True,
        )
        self.client.force_login(manager)

        missing_email = self.client.post(
            '/api/physicians/',
            data=json.dumps({'first_name': 'Ava', 'last_name': 'Patel'}),
            content_type='application/json',
        )
        self.assertEqual(missing_email.status_code, 400)
        self.assertIn('email', missing_email.json())

        created = self.client.post(
            '/api/physicians/',
            data=json.dumps({
                'first_name': 'Ava',
                'last_name': 'Patel',
                'email': '  AVA.PATEL@EXAMPLE.COM ',
                'phone_number': ' (843) 555-0123 ',
            }),
            content_type='application/json',
        )
        self.assertEqual(created.status_code, 201)
        physician = Physician.objects.select_related('user').get(
            id=created.json()['id'],
        )
        self.assertEqual(physician.user.email, 'ava.patel@example.com')
        self.assertEqual(physician.user.username, 'ava.patel@example.com')
        self.assertEqual(physician.phone_number, '(843) 555-0123')

        updated = self.client.patch(
            f'/api/physicians/{physician.id}/',
            data=json.dumps({
                'email': 'ava.new@example.com',
                'phone_number': '',
            }),
            content_type='application/json',
        )
        self.assertEqual(updated.status_code, 200)
        physician.refresh_from_db()
        physician.user.refresh_from_db()
        self.assertEqual(physician.user.username, 'ava.new@example.com')
        self.assertEqual(physician.phone_number, '')

    def test_login_accepts_email_for_legacy_username(self):
        get_user_model().objects.create_user(
            username='legacy-name',
            email='legacy@example.com',
            password='atlas',
        )

        response = self.client.post(
            '/api/login/',
            data=json.dumps({
                'username': 'LEGACY@EXAMPLE.COM',
                'password': 'atlas',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['email'], 'legacy@example.com')

    def test_login_and_logout_require_csrf_tokens_in_a_real_browser_session(self):
        get_user_model().objects.create_user(
            username='csrf-user@example.com',
            email='csrf-user@example.com',
            password='atlas',
        )
        browser = Client(enforce_csrf_checks=True)

        missing_login_token = browser.post(
            '/api/login/',
            data=json.dumps({
                'username': 'csrf-user@example.com',
                'password': 'atlas',
            }),
            content_type='application/json',
        )
        self.assertEqual(missing_login_token.status_code, 403)

        csrf_response = browser.get('/api/csrf/')
        self.assertEqual(csrf_response.status_code, 200)
        csrf_token = csrf_response.json()['csrfToken']
        self.assertTrue(csrf_token)
        self.assertIn(settings.CSRF_COOKIE_NAME, csrf_response.cookies)

        login_response = browser.post(
            '/api/login/',
            data=json.dumps({
                'username': 'csrf-user@example.com',
                'password': 'atlas',
            }),
            content_type='application/json',
            HTTP_X_CSRFTOKEN=csrf_token,
        )
        self.assertEqual(login_response.status_code, 200)
        self.assertTrue(login_response.json()['csrfToken'])

        missing_logout_token = browser.post('/api/logout/')
        self.assertEqual(missing_logout_token.status_code, 403)

        rotated_token = browser.cookies[settings.CSRF_COOKIE_NAME].value
        logout_response = browser.post(
            '/api/logout/',
            HTTP_X_CSRFTOKEN=rotated_token,
        )
        self.assertEqual(logout_response.status_code, 200)

    def test_repeated_login_attempts_are_rate_limited_by_account(self):
        cache.clear()
        browser = Client(enforce_csrf_checks=True)
        csrf_token = browser.get('/api/csrf/').json()['csrfToken']
        payload = json.dumps({
            'username': 'target@example.com',
            'password': 'incorrect',
        })

        for _ in range(5):
            response = browser.post(
                '/api/login/',
                data=payload,
                content_type='application/json',
                HTTP_X_CSRFTOKEN=csrf_token,
            )
            self.assertEqual(response.status_code, 401)

        limited = browser.post(
            '/api/login/',
            data=payload,
            content_type='application/json',
            HTTP_X_CSRFTOKEN=csrf_token,
        )
        self.assertEqual(limited.status_code, 429)
        cache.clear()

    def test_login_rotates_existing_session_identifier(self):
        get_user_model().objects.create_user(
            username='secure-login@example.com',
            email='secure-login@example.com',
            password='atlas',
        )
        session = self.client.session
        session['before_login'] = 'preserved'
        session.save()
        original_session_key = session.session_key

        response = self.client.post(
            '/api/login/',
            data=json.dumps({
                'username': 'secure-login@example.com',
                'password': 'atlas',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        authenticated_session = self.client.session
        self.assertNotEqual(authenticated_session.session_key, original_session_key)
        self.assertEqual(authenticated_session['before_login'], 'preserved')
        self.assertFalse(Session.objects.filter(session_key=original_session_key).exists())

    def test_logout_invalidates_server_side_session(self):
        user = get_user_model().objects.create_user(
            username='secure-logout@example.com',
            email='secure-logout@example.com',
            password='atlas',
        )
        self.client.force_login(user)
        authenticated_session_key = self.client.session.session_key
        self.assertTrue(Session.objects.filter(session_key=authenticated_session_key).exists())

        response = self.client.post('/api/logout/')

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Session.objects.filter(session_key=authenticated_session_key).exists())
        self.assertIn(settings.SESSION_COOKIE_NAME, response.cookies)
        self.assertEqual(response.cookies[settings.SESSION_COOKIE_NAME]['max-age'], 0)
        self.assertEqual(self.client.get('/api/me/').status_code, 403)

    def test_physician_profile_includes_current_contract_and_roles(self):
        manager = get_user_model().objects.create_user(
            username='manager@example.com',
            email='manager@example.com',
            password='atlas',
        )
        user = get_user_model().objects.create_user(
            username='ava@example.com',
            email='ava@example.com',
            first_name='Ava',
            last_name='Patel',
        )
        user.groups.add(Group.objects.create(name='Scheduler'))
        physician = Physician.objects.create(user=user, role='scheduler')
        domain = Domain.objects.create(name='Physician')
        organization = domain.region.organization
        OrganizationMembership.objects.create(
            organization=organization,
            user=manager,
            is_org_admin=True,
        )
        OrganizationMembership.objects.create(
            organization=organization,
            user=user,
        )
        contract = Contract.objects.create(domain=domain, name='Full Time')
        ContractUserAssignment.objects.create(
            contract=contract,
            physician=physician,
            domain=domain,
        )
        self.client.force_login(manager)

        response = self.client.get(f'/api/physicians/{physician.id}/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['role'], 'scheduler')
        self.assertEqual(response.json()['current_contracts'], [{
            'id': contract.id,
            'name': 'Full Time',
            'domain_id': domain.id,
            'domain': 'Physician',
        }])

    def test_create_demo_user_assigns_scheduler_role_to_existing_user(self):
        user = get_user_model().objects.create_user(username='ron', password='atlas')

        call_command('create_demo_user', verbosity=0)
        call_command('create_demo_user', verbosity=0)

        user.refresh_from_db()
        self.assertTrue(user.groups.filter(name='Scheduler').exists())

    def test_logout_does_not_stop_or_remove_background_optimizer(self):
        user = get_user_model().objects.create_user(
            username='logout-scheduler', password='atlas',
        )
        domain = Domain.objects.create(name='Logout Test Domain')
        block = ScheduleBlock.objects.create(
            start_date=timezone.localdate(),
            end_date=timezone.localdate(),
            request_open_datetime=timezone.now(),
            request_close_datetime=timezone.now(),
            build_status=ScheduleBlock.BuildStatus.BUILD,
        )
        version = ScheduleVersion.objects.create(
            schedule_block=block,
            domain=domain,
            version_number=1,
            status=ScheduleVersion.Status.BUILD,
        )
        run = OptimizerRun.objects.create(
            schedule_version=version,
            run_number=1,
            created_by=user,
            status=OptimizerRun.Status.RUNNING,
        )
        control = OptimizerControl.objects.create(
            token='19764223-ded6-4fcb-b962-b92f68962062',
            schedule_version=version,
            optimizer_run=run,
            created_by=user,
            started_at=timezone.now(),
        )
        self.client.force_login(user)

        response = self.client.post('/api/logout/')

        self.assertEqual(response.status_code, 200)
        run.refresh_from_db()
        control.refresh_from_db()
        self.assertEqual(run.status, OptimizerRun.Status.RUNNING)
        self.assertFalse(control.stop_requested)

    def test_physician_endpoints_do_not_cross_organization_boundaries(self):
        manager = get_user_model().objects.create_user('org-a-admin', password='atlas')
        own_user = get_user_model().objects.create_user(
            'own@example.com', email='own@example.com', first_name='Own', last_name='User',
        )
        foreign_user = get_user_model().objects.create_user(
            'foreign@example.com', email='foreign@example.com', first_name='Foreign', last_name='User',
        )
        own_physician = Physician.objects.create(user=own_user, phone_number='111-111-1111')
        foreign_physician = Physician.objects.create(user=foreign_user, phone_number='222-222-2222')
        own_organization = Organization.objects.create(name='Own Organization')
        foreign_organization = Organization.objects.create(name='Foreign Organization')
        own_region = Region.objects.create(organization=own_organization, name='Own Region')
        foreign_region = Region.objects.create(organization=foreign_organization, name='Foreign Region')
        own_domain = Domain.objects.create(region=own_region, name='Own Domain')
        foreign_domain = Domain.objects.create(region=foreign_region, name='Foreign Domain')
        OrganizationMembership.objects.create(
            organization=own_organization, user=manager, is_org_admin=True,
        )
        OrganizationMembership.objects.create(organization=own_organization, user=own_user)
        OrganizationMembership.objects.create(organization=foreign_organization, user=foreign_user)
        DomainMembership.objects.create(
            domain=own_domain, user=own_user, role=DomainMembership.Role.STAFF_PHYSICIAN,
        )
        DomainMembership.objects.create(
            domain=foreign_domain, user=foreign_user, role=DomainMembership.Role.STAFF_PHYSICIAN,
        )
        self.client.force_login(manager)

        listed = self.client.get('/api/physicians/')
        self.assertEqual(listed.status_code, 200)
        self.assertEqual({row['id'] for row in listed.json()}, {own_physician.id})
        self.assertEqual(self.client.get(f'/api/physicians/{foreign_physician.id}/').status_code, 403)
        self.assertEqual(
            self.client.patch(
                f'/api/physicians/{foreign_physician.id}/',
                data=json.dumps({'first_name': 'Changed'}),
                content_type='application/json',
            ).status_code,
            403,
        )
        self.assertEqual(self.client.post(f'/api/physicians/{foreign_physician.id}/disable/').status_code, 403)

    def test_profile_contact_fields_follow_domain_permissions(self):
        viewer = get_user_model().objects.create_user('directory-viewer', password='atlas')
        target = get_user_model().objects.create_user(
            'target@example.com', email='target@example.com', first_name='Target', last_name='User',
        )
        physician = Physician.objects.create(user=target, phone_number='843-555-0100')
        organization = Organization.objects.create(name='Directory Organization')
        region = Region.objects.create(organization=organization, name='Directory Region')
        domain = Domain.objects.create(region=region, name='Directory Domain')
        role = RoleTemplate.objects.create(
            region=region,
            name='Directory Without Contacts',
            permissions=['view_user_directory'],
        )
        OrganizationMembership.objects.create(organization=organization, user=viewer)
        OrganizationMembership.objects.create(organization=organization, user=target)
        DomainMembership.objects.create(
            domain=domain,
            user=viewer,
            role=DomainMembership.Role.VIEW_ONLY,
            role_template=role,
            clinically_active=False,
        )
        DomainMembership.objects.create(
            domain=domain,
            user=target,
            role=DomainMembership.Role.STAFF_PHYSICIAN,
        )
        self.client.force_login(viewer)

        response = self.client.get(f'/api/physicians/{physician.id}/')

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()['email'])
        self.assertIsNone(response.json()['phone_number'])

    def test_user_creator_must_choose_an_authorized_organization(self):
        creator = get_user_model().objects.create_user('delegated-creator', password='atlas')
        own_organization = Organization.objects.create(name='Creator Organization')
        foreign_organization = Organization.objects.create(name='Other Creator Organization')
        own_region = Region.objects.create(organization=own_organization, name='Creator Region')
        Domain.objects.create(
            region=Region.objects.create(organization=foreign_organization, name='Other Region'),
            name='Other Domain',
        )
        own_domain = Domain.objects.create(region=own_region, name='Creator Domain')
        creator_role = RoleTemplate.objects.create(
            region=own_region,
            name='User Creator',
            permissions=['create_users'],
        )
        OrganizationMembership.objects.create(organization=own_organization, user=creator)
        DomainMembership.objects.create(
            domain=own_domain,
            user=creator,
            role=DomainMembership.Role.ADMIN,
            role_template=creator_role,
            clinically_active=False,
        )
        self.client.force_login(creator)

        denied = self.client.post(
            '/api/physicians/',
            data=json.dumps({
                'organization': foreign_organization.id,
                'first_name': 'Wrong',
                'last_name': 'Organization',
                'email': 'wrong-org@example.com',
            }),
            content_type='application/json',
        )
        created = self.client.post(
            '/api/physicians/',
            data=json.dumps({
                'organization': own_organization.id,
                'first_name': 'Right',
                'last_name': 'Organization',
                'email': 'right-org@example.com',
            }),
            content_type='application/json',
        )

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(created.status_code, 201)
        self.assertTrue(OrganizationMembership.objects.filter(
            organization=own_organization,
            user_id=created.json()['user_id'],
            active=True,
        ).exists())

    def test_last_active_org_admin_cannot_be_disabled(self):
        admin = get_user_model().objects.create_user('protected-admin', password='atlas')
        physician = Physician.objects.create(user=admin)
        organization = Organization.objects.create(name='Protected Organization')
        OrganizationMembership.objects.create(
            organization=organization,
            user=admin,
            is_org_admin=True,
        )
        self.client.force_login(admin)

        response = self.client.post(f'/api/physicians/{physician.id}/disable/')

        self.assertEqual(response.status_code, 409)
        physician.refresh_from_db()
        admin.refresh_from_db()
        self.assertTrue(physician.active)
        self.assertTrue(admin.is_active)
