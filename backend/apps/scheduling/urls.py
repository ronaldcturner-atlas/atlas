from django.urls import path

from . import api

urlpatterns = [
    path("contracts/", api.contracts_list_create, name="contracts_list_create"),
    path("contracts/<int:contract_id>/", api.contract_detail, name="contract_detail"),
    path("contracts/<int:contract_id>/duplicate/", api.contract_duplicate, name="contract_duplicate"),
    path("contracts/<int:contract_id>/deactivate/", api.contract_deactivate, name="contract_deactivate"),
    path("contracts/<int:contract_id>/reactivate/", api.contract_reactivate, name="contract_reactivate"),
    path("shifts/", api.shifts_list_create, name="shifts_list_create"),
    path("shifts/<int:shift_id>/", api.shift_detail, name="shift_detail"),
    path("shift-templates/", api.shift_templates_list_create, name="shift_templates_list_create"),
    path("shift-templates/<int:template_id>/", api.shift_template_detail, name="shift_template_detail"),
    path("schedule-blocks/", api.schedule_blocks_list_create, name="schedule_blocks_list_create"),
    path("schedule-blocks/<int:block_id>/", api.schedule_block_detail, name="schedule_block_detail"),
    path("schedule-blocks/<int:block_id>/build/", api.schedule_block_build_context, name="schedule_block_build_context"),
    path("schedule-blocks/<int:block_id>/build/generate/", api.schedule_block_generate_shift_instances, name="schedule_block_generate_shift_instances"),
    path("schedule-blocks/<int:block_id>/build/versions/", api.schedule_block_schedule_versions, name="schedule_block_schedule_versions"),
    path("schedule-blocks/<int:block_id>/build/versions/<int:version_id>/shifts/", api.schedule_version_shift_instances, name="schedule_version_shift_instances"),
    path("schedule-blocks/<int:block_id>/requests/", api.schedule_block_requests_list, name="schedule_block_requests_list"),
    path("schedule-blocks/<int:block_id>/requests/context/", api.schedule_block_requests_context, name="schedule_block_requests_context"),
    path("schedule-blocks/<int:block_id>/requests/upsert/", api.schedule_block_request_upsert, name="schedule_block_request_upsert"),
    path("schedule-blocks/<int:block_id>/requests/<int:request_id>/", api.schedule_block_request_detail, name="schedule_block_request_detail"),
    path("schedule-blocks/<int:block_id>/requests/clear/", api.schedule_block_clear_requests, name="schedule_block_clear_requests"),
    path("schedule-blocks/<int:block_id>/requests/bulk/", api.schedule_block_bulk_requests, name="schedule_block_bulk_requests"),
    path("schedule-blocks/<int:block_id>/enter-preview/", api.schedule_block_enter_preview, name="schedule_block_enter_preview"),
    path("schedule-blocks/<int:block_id>/publish/", api.schedule_block_publish, name="schedule_block_publish"),
]
