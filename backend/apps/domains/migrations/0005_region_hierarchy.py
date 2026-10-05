from django.db import migrations, models
import django.db.models.deletion
import apps.domains.models


def create_regions_and_move_domains(apps, schema_editor):
    Organization = apps.get_model('domains', 'Organization')
    Region = apps.get_model('domains', 'Region')
    Domain = apps.get_model('domains', 'Domain')

    for organization in Organization.objects.all():
        region, _ = Region.objects.get_or_create(
            organization=organization,
            name=organization.name,
            defaults={'active': True},
        )
        Domain.objects.filter(organization=organization, region__isnull=True).update(region=region)


class Migration(migrations.Migration):

    dependencies = [
        ('domains', '0004_name_initial_organization'),
    ]

    operations = [
        migrations.CreateModel(
            name='Region',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=160)),
                ('active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='regions', to='domains.organization')),
            ],
            options={'ordering': ['organization__name', 'name']},
        ),
        migrations.AddField(
            model_name='domain',
            name='region',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, related_name='domains', to='domains.region'),
        ),
        migrations.RunPython(create_regions_and_move_domains, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name='domain',
            name='unique_domain_name_per_organization',
        ),
        migrations.RemoveField(
            model_name='domain',
            name='organization',
        ),
        migrations.AlterField(
            model_name='domain',
            name='region',
            field=models.ForeignKey(default=apps.domains.models.get_default_region_id, on_delete=django.db.models.deletion.CASCADE, related_name='domains', to='domains.region'),
        ),
        migrations.AddConstraint(
            model_name='region',
            constraint=models.UniqueConstraint(fields=('organization', 'name'), name='unique_region_name_per_organization'),
        ),
        migrations.AddConstraint(
            model_name='domain',
            constraint=models.UniqueConstraint(fields=('region', 'name'), name='unique_domain_name_per_region'),
        ),
    ]
