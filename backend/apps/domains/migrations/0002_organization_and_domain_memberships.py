from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import apps.domains.models


def create_initial_organization_and_memberships(apps, schema_editor):
    Organization = apps.get_model('domains', 'Organization')
    OrganizationMembership = apps.get_model('domains', 'OrganizationMembership')
    Domain = apps.get_model('domains', 'Domain')
    DomainMembership = apps.get_model('domains', 'DomainMembership')
    Physician = apps.get_model('accounts', 'Physician')
    User = apps.get_model('auth', 'User')

    organization, _ = Organization.objects.get_or_create(name='Atlas Organization')
    Domain.objects.filter(organization__isnull=True).update(organization=organization)

    privileged_group_names = {'admin', 'scheduler'}
    for user in User.objects.prefetch_related('groups').all():
        group_names = {group.name.strip().lower() for group in user.groups.all()}
        organization_role = (
            'org_admin'
            if user.is_superuser or user.is_staff or group_names & privileged_group_names
            else 'member'
        )
        OrganizationMembership.objects.get_or_create(
            organization=organization,
            user=user,
            defaults={'role': organization_role},
        )

    role_map = {
        'org_admin': 'admin',
        'medical_director': 'medical_director',
        'admin': 'admin',
        'staff_physician': 'staff_physician',
        'app': 'app',
        'scheduler': 'scheduler',
    }
    domains = list(Domain.objects.filter(organization=organization))
    for physician in Physician.objects.select_related('user').all():
        default_role = 'app' if physician.clinician_type in {'pa', 'np'} else 'staff_physician'
        domain_role = role_map.get(physician.role, default_role)
        for domain in domains:
            DomainMembership.objects.get_or_create(
                domain=domain,
                user=physician.user,
                defaults={'role': domain_role},
            )

    org_admins = OrganizationMembership.objects.filter(
        organization=organization,
        role='org_admin',
    ).select_related('user')
    for membership in org_admins:
        for domain in domains:
            DomainMembership.objects.get_or_create(
                domain=domain,
                user=membership.user,
                defaults={'role': 'admin'},
            )


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0004_physician_role'),
        ('domains', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Organization',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=160, unique=True)),
                ('active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={'ordering': ['name']},
        ),
        migrations.AddField(
            model_name='domain',
            name='organization',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='domains',
                to='domains.organization',
            ),
        ),
        migrations.AlterField(
            model_name='domain',
            name='name',
            field=models.CharField(max_length=120),
        ),
        migrations.CreateModel(
            name='OrganizationMembership',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('role', models.CharField(choices=[('org_admin', 'Org Admin'), ('member', 'Member')], default='member', max_length=20)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='memberships', to='domains.organization')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='organization_memberships', to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ['user__last_name', 'user__first_name', 'user__username']},
        ),
        migrations.CreateModel(
            name='DomainMembership',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('role', models.CharField(choices=[('medical_director', 'Medical Director'), ('admin', 'Admin'), ('staff_physician', 'Staff Physician'), ('app', 'APP'), ('scheduler', 'Scheduler'), ('view_only', 'View Only')], max_length=30)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('domain', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='memberships', to='domains.domain')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='domain_memberships', to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ['domain__name', 'user__last_name', 'user__first_name']},
        ),
        migrations.RunPython(create_initial_organization_and_memberships, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='domain',
            name='organization',
            field=models.ForeignKey(default=apps.domains.models.get_default_organization_id, on_delete=django.db.models.deletion.CASCADE, related_name='domains', to='domains.organization'),
        ),
        migrations.AddConstraint(
            model_name='domain',
            constraint=models.UniqueConstraint(fields=('organization', 'name'), name='unique_domain_name_per_organization'),
        ),
        migrations.AddConstraint(
            model_name='organizationmembership',
            constraint=models.UniqueConstraint(fields=('organization', 'user'), name='unique_user_membership_per_organization'),
        ),
        migrations.AddConstraint(
            model_name='domainmembership',
            constraint=models.UniqueConstraint(fields=('domain', 'user'), name='unique_user_membership_per_domain'),
        ),
    ]
