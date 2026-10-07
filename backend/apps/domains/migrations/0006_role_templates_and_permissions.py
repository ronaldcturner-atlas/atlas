from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


CLINICAL = [
    'view_published_schedules', 'view_preview', 'view_domain_statistics',
    'view_user_directory', 'view_phone_numbers', 'view_email_addresses',
    'post_own_shifts', 'offer_own_shift', 'propose_trade', 'split_own_shift',
    'pick_up_shifts', 'submit_own_requests',
]
SCHEDULER = CLINICAL + [
    'manage_date_comments', 'manage_published_assignments',
    'split_any_published_shift', 'modify_any_published_shift_times',
    'manage_any_shift_posting', 'manage_user_offers_trades',
    'approve_pickups_trades', 'send_urgent_shift_notifications',
    'batch_update_published_shifts', 'manage_build_workspace',
    'administer_requests', 'publish_schedule', 'unpublish_schedule',
    'manage_shift_templates', 'view_audit_history',
]
DOMAIN_ADMIN = SCHEDULER + [
    'create_users', 'edit_user_profiles', 'manage_domain_access',
    'change_clinical_status', 'assign_domain_roles',
]
REGIONAL_ADMIN = DOMAIN_ADMIN + [
    'manage_regional_facilities', 'manage_domains', 'suspend_region_users',
    'view_roles', 'create_roles', 'edit_roles', 'activate_roles',
    'delete_unused_roles', 'delegate_role_management',
]
ROLE_DEFINITIONS = {
    'regional_admin': ('Regional Admin', REGIONAL_ADMIN),
    'domain_admin': ('Domain Admin', DOMAIN_ADMIN),
    'scheduler': ('Scheduler', SCHEDULER),
    'medical_director': ('Medical Director', sorted(set(CLINICAL + ['manage_date_comments']))),
    'staff_physician': ('Staff Physician', CLINICAL),
    'app': ('APP', CLINICAL),
    'view_only': ('View Only', ['view_published_schedules', 'view_preview']),
}


def create_default_roles(apps, schema_editor):
    RoleTemplate = apps.get_model('domains', 'RoleTemplate')
    DomainMembership = apps.get_model('domains', 'DomainMembership')
    OrganizationMembership = apps.get_model('domains', 'OrganizationMembership')
    Region = apps.get_model('domains', 'Region')

    for region in Region.objects.all():
        templates = {}
        for key, (name, permissions) in ROLE_DEFINITIONS.items():
            template, _ = RoleTemplate.objects.get_or_create(
                region=region,
                name=name,
                defaults={
                    'system_key': key,
                    'permissions': permissions,
                    'notification_defaults': {},
                    'active': True,
                },
            )
            templates[key] = template

        for membership in DomainMembership.objects.filter(domain__region=region).select_related('domain'):
            legacy_role = membership.role
            if legacy_role == 'org_admin':
                OrganizationMembership.objects.filter(
                    organization=region.organization,
                    user=membership.user,
                ).update(is_org_admin=True, active=True)
                next_key = 'regional_admin'
            elif legacy_role == 'admin':
                next_key = 'domain_admin'
            else:
                next_key = legacy_role if legacy_role in templates else 'view_only'
            membership.role = next_key
            membership.role_template = templates[next_key]
            membership.clinically_active = next_key != 'view_only'
            membership.active = True
            membership.save(update_fields=['role', 'role_template', 'clinically_active', 'active'])


class Migration(migrations.Migration):

    dependencies = [
        ('domains', '0005_region_hierarchy'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='organizationmembership',
            name='active',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='organizationmembership',
            name='is_org_admin',
            field=models.BooleanField(default=False),
        ),
        migrations.CreateModel(
            name='RoleTemplate',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100)),
                ('system_key', models.SlugField(blank=True, max_length=80)),
                ('permissions', models.JSONField(default=list)),
                ('notification_defaults', models.JSONField(blank=True, default=dict)),
                ('active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('last_unassigned_at', models.DateTimeField(blank=True, null=True)),
                ('region', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='role_templates', to='domains.region')),
            ],
            options={'ordering': ['region__name', 'name', 'id']},
        ),
        migrations.AddConstraint(
            model_name='roletemplate',
            constraint=models.UniqueConstraint(fields=('region', 'name'), name='unique_role_template_name_per_region'),
        ),
        migrations.AddField(
            model_name='domainmembership',
            name='active',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='domainmembership',
            name='clinically_active',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='domainmembership',
            name='role_template',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='memberships', to='domains.roletemplate'),
        ),
        migrations.AlterField(
            model_name='domainmembership',
            name='role',
            field=models.CharField(choices=[('org_admin', 'Org Admin'), ('medical_director', 'Medical Director'), ('admin', 'Admin'), ('staff_physician', 'Staff Physician'), ('app', 'APP'), ('scheduler', 'Scheduler'), ('view_only', 'View Only'), ('regional_admin', 'Regional Admin'), ('domain_admin', 'Domain Admin')], max_length=30),
        ),
        migrations.CreateModel(
            name='AuditEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('action', models.CharField(max_length=120)),
                ('details', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('actor', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='atlas_audit_events', to=settings.AUTH_USER_MODEL)),
                ('domain', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='audit_events', to='domains.domain')),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='audit_events', to='domains.organization')),
                ('region', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='audit_events', to='domains.region')),
                ('target_user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='atlas_target_audit_events', to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ['-created_at', '-id']},
        ),
        migrations.RunPython(create_default_roles, migrations.RunPython.noop),
    ]
