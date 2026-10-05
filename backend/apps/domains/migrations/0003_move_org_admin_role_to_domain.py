from django.db import migrations, models


def move_org_admin_roles(apps, schema_editor):
    OrganizationMembership = apps.get_model('domains', 'OrganizationMembership')
    DomainMembership = apps.get_model('domains', 'DomainMembership')

    for membership in OrganizationMembership.objects.filter(role='org_admin').select_related('organization'):
        domain_membership = DomainMembership.objects.filter(
            domain__organization=membership.organization,
            user=membership.user,
        ).order_by('domain_id').first()
        if domain_membership:
            domain_membership.role = 'org_admin'
            domain_membership.save(update_fields=['role'])


class Migration(migrations.Migration):

    dependencies = [
        ('domains', '0002_organization_and_domain_memberships'),
    ]

    operations = [
        migrations.AlterField(
            model_name='domainmembership',
            name='role',
            field=models.CharField(
                choices=[
                    ('org_admin', 'Org Admin'),
                    ('medical_director', 'Medical Director'),
                    ('admin', 'Admin'),
                    ('staff_physician', 'Staff Physician'),
                    ('app', 'APP'),
                    ('scheduler', 'Scheduler'),
                    ('view_only', 'View Only'),
                ],
                max_length=30,
            ),
        ),
        migrations.RunPython(move_org_admin_roles, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='organizationmembership',
            name='role',
        ),
    ]
