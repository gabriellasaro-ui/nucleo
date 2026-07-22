from datetime import timedelta

from django.db import migrations, models
from django.db.models import F, Q


def mark_existing_history(apps, schema_editor):
    conversation = apps.get_model("crm", "WhatsAppConversation")
    conversation.objects.filter(
        Q(remote_jid__endswith="@lid")
        | Q(
            contact__isnull=True,
            last_message_at__lt=F("created_at") - timedelta(hours=1),
        )
    ).update(is_history_import=True)


class Migration(migrations.Migration):
    dependencies = [
        ("crm", "0008_whatsappconversation_whatsappmessage_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="whatsappconversation",
            name="is_history_import",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(mark_existing_history, migrations.RunPython.noop),
    ]
