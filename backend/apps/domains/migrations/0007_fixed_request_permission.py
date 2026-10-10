from django.db import migrations


PERMISSION = 'manage_fixed_requests'
DEFAULT_SYSTEM_ROLES = {'scheduler', 'domain_admin', 'regional_admin'}


def add_fixed_request_permission(apps, schema_editor):
    RoleTemplate = apps.get_model('domains', 'RoleTemplate')
    for role in RoleTemplate.objects.filter(system_key__in=DEFAULT_SYSTEM_ROLES):
        permissions = set(role.permissions or [])
        permissions.add(PERMISSION)
        role.permissions = sorted(permissions)
        role.save(update_fields=['permissions', 'updated_at'])


def remove_fixed_request_permission(apps, schema_editor):
    RoleTemplate = apps.get_model('domains', 'RoleTemplate')
    for role in RoleTemplate.objects.filter(system_key__in=DEFAULT_SYSTEM_ROLES):
        permissions = set(role.permissions or [])
        permissions.discard(PERMISSION)
        role.permissions = sorted(permissions)
        role.save(update_fields=['permissions', 'updated_at'])


class Migration(migrations.Migration):
    dependencies = [
        ('domains', '0006_role_templates_and_permissions'),
    ]

    operations = [
        migrations.RunPython(add_fixed_request_permission, remove_fixed_request_permission),
    ]
