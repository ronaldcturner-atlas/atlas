import os

from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounts.models import Physician
from apps.accounts.security import set_temporary_password
from apps.domains.models import Domain, Organization, OrganizationMembership, Region, RoleTemplate
from apps.domains.permissions import DEFAULT_ROLE_DEFINITIONS


class Command(BaseCommand):
    help = 'Assign the first active Org Admin without exposing a password in command history.'

    def add_arguments(self, parser):
        organization = parser.add_mutually_exclusive_group(required=True)
        organization.add_argument('--organization-id', type=int)
        organization.add_argument('--organization-name')
        parser.add_argument('--region-name')
        parser.add_argument('--domain-name', default='Physician')
        parser.add_argument('--email', required=True)
        parser.add_argument('--first-name', default='')
        parser.add_argument('--last-name', default='')

    @transaction.atomic
    def handle(self, *args, **options):
        organization = self._organization(options)
        if OrganizationMembership.objects.filter(
            organization=organization,
            active=True,
            is_org_admin=True,
            user__is_active=True,
        ).exists():
            raise CommandError(
                'This organization already has an active Org Admin. Use the application to assign additional Org Admins.'
            )

        temporary_password = os.environ.get(
            'INITIAL_ORG_ADMIN_PASSWORD', '',
        )
        if not temporary_password:
            raise CommandError(
                'Set INITIAL_ORG_ADMIN_PASSWORD for this one-time command, then remove it immediately.'
            )

        email = options['email'].strip().lower()
        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            user = User(
                username=email,
                email=email,
                first_name=options['first_name'].strip(),
                last_name=options['last_name'].strip(),
                is_active=True,
            )
        else:
            user.is_active = True
            user.username = email
            user.email = email
            if options['first_name']:
                user.first_name = options['first_name'].strip()
            if options['last_name']:
                user.last_name = options['last_name'].strip()
        try:
            validate_password(temporary_password, user=user)
        except ValidationError as exc:
            raise CommandError(' '.join(exc.messages)) from exc
        user.save()
        physician, _created = Physician.objects.get_or_create(
            user=user,
            defaults={'active': True},
        )
        if not physician.active:
            physician.active = True
            physician.save(update_fields=['active'])
        membership, _created = OrganizationMembership.objects.get_or_create(
            organization=organization,
            user=user,
        )
        membership.active = True
        membership.is_org_admin = True
        membership.save(update_fields=['active', 'is_org_admin', 'updated_at'])
        set_temporary_password(user, temporary_password)
        self.stdout.write(self.style.SUCCESS(
            f'Assigned {email} as the first Org Admin for {organization.name}. '
            'Remove INITIAL_ORG_ADMIN_PASSWORD now; the user must change it at first login.'
        ))

    def _organization(self, options):
        organization_id = options.get('organization_id')
        if organization_id:
            organization = Organization.objects.filter(
                id=organization_id,
                active=True,
            ).first()
            if organization is None:
                raise CommandError('Choose an active organization.')
            return organization

        organization_name = (options.get('organization_name') or '').strip()
        if not organization_name:
            raise CommandError('Organization name is required.')
        organization = Organization.objects.filter(name__iexact=organization_name).first()
        if organization is None:
            if OrganizationMembership.objects.filter(active=True).exists():
                raise CommandError(
                    'An initialized Organization already exists. Choose it with --organization-id instead of creating another.'
                )
            organization = Organization.objects.create(name=organization_name, active=True)
        elif not organization.active:
            raise CommandError('Choose an active organization.')

        region_name = (options.get('region_name') or organization.name).strip()
        domain_name = (options.get('domain_name') or '').strip()
        if not region_name or not domain_name:
            raise CommandError('Region and Domain names are required.')
        region, _created = Region.objects.get_or_create(
            organization=organization,
            name=region_name,
            defaults={'active': True},
        )
        if not region.active:
            raise CommandError('The initial Region must be active.')
        Domain.objects.get_or_create(
            region=region,
            name=domain_name,
            defaults={'active': True},
        )
        for system_key, name, permissions in DEFAULT_ROLE_DEFINITIONS:
            RoleTemplate.objects.get_or_create(
                region=region,
                name=name,
                defaults={
                    'system_key': system_key,
                    'permissions': sorted(permissions),
                    'notification_defaults': {},
                    'active': True,
                },
            )
        return organization
