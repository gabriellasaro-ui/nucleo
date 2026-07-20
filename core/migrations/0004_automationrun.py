from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0003_automation_canvas"),
    ]

    operations = [
        migrations.CreateModel(
            name="AutomationRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("success", "Sucesso"), ("skipped", "Ignorada"), ("scheduled", "Agendada"), ("error", "Erro"), ("test", "Teste")], default="success", max_length=12)),
                ("object_model", models.CharField(blank=True, max_length=40)),
                ("object_id", models.PositiveIntegerField(blank=True, null=True)),
                ("object_repr", models.CharField(blank=True, max_length=200)),
                ("summary", models.CharField(blank=True, max_length=300)),
                ("path", models.JSONField(blank=True, default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("automation", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="runs", to="core.automation")),
                ("event", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="automation_runs", to="core.event")),
                ("workspace", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="automation_runs", to="core.workspace")),
            ],
            options={
                "verbose_name": "Execução de automação",
                "verbose_name_plural": "Execuções de automação",
                "ordering": ["-created_at"],
            },
        ),
    ]
