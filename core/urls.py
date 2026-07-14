from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("workspaces/new/", views.workspace_new, name="workspace_new"),
    path("workspaces/<int:pk>/switch/", views.workspace_switch, name="workspace_switch"),
    path("settings/members/", views.members, name="members"),
    path("settings/members/add/", views.member_add, name="member_add"),
    path("settings/members/<int:pk>/role/", views.member_update, name="member_update"),
    path("settings/members/<int:pk>/remove/", views.member_remove, name="member_remove"),
    path("settings/fields/", views.custom_fields, name="custom_fields"),
    path("settings/fields/add/", views.custom_field_add, name="custom_field_add"),
    path("settings/fields/<int:pk>/edit/", views.custom_field_update, name="custom_field_update"),
    path("settings/fields/<int:pk>/delete/", views.custom_field_delete, name="custom_field_delete"),
    path("settings/automations/", views.automations, name="automations"),
    path("settings/automations/add/", views.automation_add, name="automation_add"),
    path("settings/automations/<int:pk>/toggle/", views.automation_toggle, name="automation_toggle"),
    path("settings/automations/<int:pk>/delete/", views.automation_delete, name="automation_delete"),
    path("settings/appearance/", views.appearance, name="appearance"),
]
