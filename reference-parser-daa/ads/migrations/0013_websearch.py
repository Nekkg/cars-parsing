import uuid
from django.db import migrations, models
from django.utils import timezone


class Migration(migrations.Migration):
    dependencies = [('ads', '0012_searchjob_kind_alter_advertisement_created_at')]
    operations = [migrations.CreateModel(name='WebSearch', fields=[
        ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
        ('owner_key', models.CharField(db_index=True, max_length=64)),
        ('filters', models.JSONField(default=dict)),
        ('collect_until', models.DateTimeField(db_index=True)),
        ('created_at', models.DateTimeField(db_index=True, default=timezone.now)),
        ('search_job', models.ForeignKey(on_delete=models.CASCADE, to='ads.searchjob')),
    ])]
