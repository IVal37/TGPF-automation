# imports from std lib
import os

# imports from third party
from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'TGPFflows.settings')

app = Celery('TGPFflows')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()
