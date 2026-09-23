from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from ..config import settings

celery = Celery("viljaops", broker=settings.redis_url, backend=settings.redis_url)

celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_soft_time_limit=600,
    task_time_limit=900,
    task_routes={
        "viljaops.analyze_repo": {"queue": "analysis"},
        "viljaops.analyze_deployment": {"queue": "rca"},
        "viljaops.sweep_anomalies": {"queue": "infra"},
        "viljaops.refresh_risk": {"queue": "infra"},
        "viljaops.prune_logs": {"queue": "infra"},
        "viljaops.verify_fixes": {"queue": "rca"},
    },
    beat_schedule={
        "sweep-anomalies": {"task": "viljaops.sweep_anomalies", "schedule": crontab(minute="*/5")},
        "refresh-risk": {"task": "viljaops.refresh_risk", "schedule": crontab(minute=0, hour="*/2")},
        "prune-old-logs": {"task": "viljaops.prune_logs", "schedule": crontab(minute=30, hour=3)},
        # Grace windows are as short as 3 minutes, so this needs to run often.
        "verify-fixes": {"task": "viljaops.verify_fixes", "schedule": crontab(minute="*/1")},
    },
)

celery.autodiscover_tasks(["app.workers"])

from . import tasks  # noqa: E402,F401  (register tasks)
