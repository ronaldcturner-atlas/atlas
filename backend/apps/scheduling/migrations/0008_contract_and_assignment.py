from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0002_physician_active_physician_clinician_type_and_more'),
        ('domains', '0001_initial'),
        ('facilities', '0003_facility_short_name'),
        ('scheduling', '0007_schedulerequest'),
    ]

    operations = [
        migrations.CreateModel(
            name='Contract',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=255)),
                ('active', models.BooleanField(default=True)),
                ('workload_settings', models.JSONField(blank=True, default=dict)),
                ('shift_settings', models.JSONField(blank=True, default=dict)),
                ('night_settings', models.JSONField(blank=True, default=dict)),
                ('weekend_settings', models.JSONField(blank=True, default=dict)),
                ('request_settings', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('domain', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='contracts', to='domains.domain')),
                ('facilities', models.ManyToManyField(blank=True, related_name='contracts', to='facilities.facility')),
            ],
            options={
                'ordering': ['domain__name', 'name', '-id'],
            },
        ),
        migrations.CreateModel(
            name='ContractUserAssignment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('contract', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='user_assignments', to='scheduling.contract')),
                ('domain', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='contract_user_assignments', to='domains.domain')),
                ('physician', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='contract_assignments', to='accounts.physician')),
            ],
            options={
                'ordering': ['physician__user__last_name', 'physician__user__first_name'],
            },
        ),
        migrations.AddConstraint(
            model_name='contract',
            constraint=models.UniqueConstraint(fields=('domain', 'name'), name='unique_contract_name_per_domain'),
        ),
        migrations.AddConstraint(
            model_name='contractuserassignment',
            constraint=models.UniqueConstraint(fields=('contract', 'physician'), name='unique_physician_per_contract_assignment'),
        ),
        migrations.AddConstraint(
            model_name='contractuserassignment',
            constraint=models.UniqueConstraint(fields=('domain', 'physician'), name='unique_physician_default_contract_per_domain'),
        ),
    ]
