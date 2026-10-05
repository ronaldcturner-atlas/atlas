from django.db import migrations


def name_initial_organization(apps, schema_editor):
    Organization = apps.get_model('domains', 'Organization')
    organization = Organization.objects.filter(name='Atlas Organization').first()
    if organization and not Organization.objects.filter(name='Lowcountry Emergency Physicians').exists():
        organization.name = 'Lowcountry Emergency Physicians'
        organization.save(update_fields=['name'])


class Migration(migrations.Migration):

    dependencies = [
        ('domains', '0003_move_org_admin_role_to_domain'),
    ]

    operations = [
        migrations.RunPython(name_initial_organization, migrations.RunPython.noop),
    ]
