from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('ads','0006_advertisement_brand_key_advertisement_model_key')]
    operations = [
        migrations.AddField(model_name='advertisement',name='generation_key',
            field=models.CharField(blank=True,db_index=True,default='',max_length=300)),
        migrations.AddField(model_name='advertisement',name='generation_evidence',
            field=models.JSONField(blank=True,default=dict)),
    ]
