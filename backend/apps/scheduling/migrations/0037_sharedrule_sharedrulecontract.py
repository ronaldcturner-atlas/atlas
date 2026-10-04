from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('domains', '0001_initial'),
        ('scheduling', '0036_optimizerrun_optimization_focus'),
    ]

    operations = [
        migrations.CreateModel(
            name='SharedRule',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=255)),
                ('active', models.BooleanField(default=True)),
                ('period_type', models.CharField(choices=[('WEEK', 'Week'), ('MONTH', 'Month'), ('SCHEDULE_BLOCK', 'Schedule Block')], default='SCHEDULE_BLOCK', max_length=20)),
                ('units', models.CharField(choices=[('HOURS', 'Hours'), ('SHIFTS', 'Shifts')], default='SHIFTS', max_length=10)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('domain', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='shared_rules', to='domains.domain')),
                ('shift_templates', models.ManyToManyField(related_name='shared_rules', to='scheduling.shifttemplate')),
            ],
            options={'ordering': ['domain__name', 'name', 'id']},
        ),
        migrations.CreateModel(
            name='SharedRuleContract',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('enabled', models.BooleanField(default=True)),
                ('min_value', models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True)),
                ('max_value', models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True)),
                ('min_penalty_weight', models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ('max_penalty_weight', models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ('spread_violations', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('contract', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='shared_rule_links', to='scheduling.contract')),
                ('shared_rule', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='contract_links', to='scheduling.sharedrule')),
            ],
            options={'ordering': ['contract__name', 'id']},
        ),
        migrations.AddField(
            model_name='sharedrule',
            name='contracts',
            field=models.ManyToManyField(related_name='shared_rules', through='scheduling.SharedRuleContract', to='scheduling.contract'),
        ),
        migrations.AddConstraint(
            model_name='sharedrule',
            constraint=models.UniqueConstraint(fields=('domain', 'name'), name='unique_shared_rule_name_per_domain'),
        ),
        migrations.AddConstraint(
            model_name='sharedrulecontract',
            constraint=models.UniqueConstraint(fields=('shared_rule', 'contract'), name='unique_contract_per_shared_rule'),
        ),
    ]
