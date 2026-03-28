# Generated migration — adds client_data_json field to BiometricKey
# Required for proper WebAuthn verification (BUG-FIX)

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('idcard_app', '0005_announcement'),
    ]

    operations = [
        migrations.AddField(
            model_name='biometrickey',
            name='client_data_json',
            field=models.TextField(
                blank=True,
                default='',
                help_text='WebAuthn clientDataJSON from registration'
            ),
            preserve_default=False,
        ),
    ]
