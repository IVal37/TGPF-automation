from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='driver',
            name='is_active',
            field=models.BooleanField(default=False),
        ),
    ]
