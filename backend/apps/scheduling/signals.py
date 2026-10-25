from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import ScheduleRequestGroup, ScheduleRequestGroupMember


@receiver(post_delete, sender=ScheduleRequestGroupMember)
def mark_empty_request_group_for_attention(sender, instance, **kwargs):
    group = ScheduleRequestGroup.objects.filter(id=instance.group_id).first()
    if group is not None:
        ScheduleRequestGroup.objects.filter(id=group.id).update(
            revision=group.revision + 1,
            needs_attention=not group.memberships.exists(),
        )
