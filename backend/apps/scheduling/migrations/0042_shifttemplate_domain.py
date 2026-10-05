import apps.domains.models
import django.db.models.deletion
from django.db import migrations, models


def assign_existing_templates(apps, schema_editor):
    Domain = apps.get_model('domains', 'Domain')
    ShiftTemplate = apps.get_model('scheduling', 'ShiftTemplate')
    if not ShiftTemplate.objects.filter(domain__isnull=True).exists():
        return
    domain = Domain.objects.filter(name='Physician').order_by('id').first()
    if domain is None:
        domain = Domain.objects.order_by('id').first()
    if domain is None:
        Organization = apps.get_model('domains', 'Organization')
        Region = apps.get_model('domains', 'Region')
        organization = Organization.objects.create(name='Lowcountry Emergency Physicians', active=True)
        region = Region.objects.create(organization=organization, name=organization.name, active=True)
        domain = Domain.objects.create(region=region, name='Atlas Default Domain', active=True)
    ShiftTemplate.objects.filter(domain__isnull=True).update(domain=domain)


class Migration(migrations.Migration):

    dependencies = [
        ('domains', '0005_region_hierarchy'),
        ('facilities', '0005_facility_region'),
        ('scheduling', '0041_schedulecommentseries'),
    ]

    operations = [
        migrations.AddField(
            model_name='shifttemplate',
            name='domain',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='shift_templates',
                to='domains.domain',
            ),
        ),
        migrations.RunPython(assign_existing_templates, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='shifttemplate',
            name='domain',
            field=models.ForeignKey(
                default=apps.domains.models.get_default_domain_id,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='shift_templates',
                to='domains.domain',
            ),
        ),
    ]
