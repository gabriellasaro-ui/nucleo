from django.urls import path

from . import views

app_name = "crm"

urlpatterns = [
    # Companies
    path("companies/", views.company_list, name="company_list"),
    path("companies/rows/", views.company_rows, name="company_rows"),
    path("companies/new/", views.company_form, name="company_create"),
    path("companies/<int:pk>/", views.company_detail, name="company_detail"),
    path("companies/<int:pk>/edit/", views.company_form, name="company_edit"),
    path("companies/<int:pk>/delete/", views.company_delete, name="company_delete"),
    # Contacts
    path("contacts/", views.contact_list, name="contact_list"),
    path("contacts/rows/", views.contact_rows, name="contact_rows"),
    path("contacts/new/", views.contact_form, name="contact_create"),
    path("contacts/<int:pk>/", views.contact_detail, name="contact_detail"),
    path("contacts/<int:pk>/edit/", views.contact_form, name="contact_edit"),
    path("contacts/<int:pk>/delete/", views.contact_delete, name="contact_delete"),
    # Deals (kanban)
    path("deals/", views.deal_board, name="deal_board"),
    path("deals/cards/", views.deal_board_cards, name="deal_cards"),
    path("deals/stats/", views.deal_stats, name="deal_stats"),
    path("deals/new/", views.deal_form, name="deal_create"),
    path("deals/<int:pk>/", views.deal_detail, name="deal_detail"),
    path("deals/<int:pk>/workspace/", views.deal_workspace, name="deal_workspace"),
    path("deals/<int:pk>/edit/", views.deal_form, name="deal_edit"),
    path("deals/<int:pk>/delete/", views.deal_delete, name="deal_delete"),
    path("deals/<int:pk>/move/", views.deal_move, name="deal_move"),
    # Pipelines & stages (columns)
    path("pipelines/new/", views.pipeline_create, name="pipeline_create"),
    path("pipelines/<int:pk>/customize/", views.pipeline_customize, name="pipeline_customize"),
    path("pipelines/<int:pk>/update/", views.pipeline_update, name="pipeline_update"),
    path("pipelines/<int:pk>/fields/add/", views.pipeline_field_add, name="pipeline_field_add"),
    path("pipelines/<int:pk>/fields/<int:field_pk>/edit/", views.pipeline_field_update, name="pipeline_field_update"),
    path("pipelines/<int:pk>/fields/<int:field_pk>/delete/", views.pipeline_field_delete, name="pipeline_field_delete"),
    path("pipelines/stages/add/", views.stage_add, name="stage_add"),
    path("pipelines/stages/<int:pk>/fields/add/", views.stage_field_add, name="stage_field_add"),
    path("pipelines/stages/reorder/", views.stage_reorder, name="stage_reorder"),
    path("pipelines/stages/<int:pk>/edit/", views.stage_update, name="stage_update"),
    path("pipelines/stages/<int:pk>/delete/", views.stage_delete, name="stage_delete"),
    # Activities (timeline)
    path("tasks/", views.tasks, name="tasks"),
    path("tasks/new/", views.task_create, name="task_create"),
    path("tasks/<int:pk>/toggle/", views.task_toggle, name="task_toggle"),
    path("activities/new/", views.activity_create, name="activity_create"),
    path("activities/<int:pk>/toggle/", views.activity_toggle, name="activity_toggle"),
    path("activities/<int:pk>/delete/", views.activity_delete, name="activity_delete"),
    # Attachments
    path("attachments/new/", views.attachment_upload, name="attachment_upload"),
    path("attachments/<int:pk>/delete/", views.attachment_delete, name="attachment_delete"),
]
