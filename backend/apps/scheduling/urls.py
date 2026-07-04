from django.urls import path

from . import api

urlpatterns = [
    path("shifts/", api.shifts_list_create, name="shifts_list_create"),
    path("shifts/<int:shift_id>/", api.shift_detail, name="shift_detail"),
    path("shift-templates/", api.shift_templates_list_create, name="shift_templates_list_create"),
    path("shift-templates/<int:template_id>/", api.shift_template_detail, name="shift_template_detail"),
    path("schedule-blocks/", api.schedule_blocks_list_create, name="schedule_blocks_list_create"),
    path("schedule-blocks/<int:block_id>/", api.schedule_block_detail, name="schedule_block_detail"),
    path("schedule-blocks/<int:block_id>/enter-preview/", api.schedule_block_enter_preview, name="schedule_block_enter_preview"),
    path("schedule-blocks/<int:block_id>/publish/", api.schedule_block_publish, name="schedule_block_publish"),
]
