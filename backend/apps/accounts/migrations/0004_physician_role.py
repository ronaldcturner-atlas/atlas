from django.db import migrations, models


def populate_existing_roles(apps, schema_editor):
    Physician = apps.get_model('accounts', 'Physician')
    role_by_group = {
        'org admin': 'org_admin',
        'medical director': 'medical_director',
        'admin': 'admin',
        'staff physician': 'staff_physician',
        'app': 'app',
        'scheduler': 'scheduler',
    }
    for physician in Physician.objects.prefetch_related('user__groups').iterator(chunk_size=200):
        group_names = {group.name.strip().lower() for group in physician.user.groups.all()}
        role = next((value for name, value in role_by_group.items() if name in group_names), '')
        if role:
            physician.role = role
            physician.save(update_fields=['role'])


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0003_physician_phone_number'),
    ]

    operations = [
        migrations.AddField(
            model_name='physician',
            name='role',
            field=models.CharField(
                blank=True,
                choices=[
                    ('org_admin', 'Org Admin'),
                    ('medical_director', 'Medical Director'),
                    ('admin', 'Admin'),
                    ('staff_physician', 'Staff Physician'),
                    ('app', 'APP'),
                    ('scheduler', 'Scheduler'),
                ],
                max_length=30,
            ),
        ),
        migrations.RunPython(populate_existing_roles, migrations.RunPython.noop),
    ]
