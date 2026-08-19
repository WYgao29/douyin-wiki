from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .service import DouyinWikiService


class Worker:
    def __init__(self, service: DouyinWikiService) -> None:
        self.service = service
        self.worker_id = f"worker-{uuid.uuid4().hex}"
        self.max_parallel_jobs = max(
            2,
            service.config.worker.download_concurrency
            + service.config.worker.media_concurrency
            + service.config.worker.analysis_concurrency,
        )

    async def run_once(self):
        self.service.database.recover_expired_jobs()
        job = self.service.database.claim_next_job(
            worker_id=self.worker_id,
            lease_seconds=self.service.config.worker.lease_seconds,
        )
        if job is None:
            return None
        return await self._process_with_heartbeat(job)

    async def run_forever(self) -> None:
        self.service.database.recover_expired_jobs()
        self.run_due_maintenance()
        running: set[asyncio.Task] = set()
        while True:
            completed = {task for task in running if task.done()}
            for task in completed:
                task.result()
            running -= completed
            self.service.database.recover_expired_jobs()
            while len(running) < self.max_parallel_jobs:
                job = self.service.database.claim_next_job(
                    worker_id=self.worker_id,
                    lease_seconds=self.service.config.worker.lease_seconds,
                )
                if job is None:
                    break
                task = asyncio.create_task(self._process_with_heartbeat(job))
                running.add(task)
            if running:
                await asyncio.wait(running, timeout=self.service.config.worker.poll_seconds)
            else:
                await asyncio.sleep(self.service.config.worker.poll_seconds)

    async def _process_with_heartbeat(self, job):
        stop = asyncio.Event()

        async def heartbeat() -> None:
            while True:
                try:
                    await asyncio.wait_for(
                        stop.wait(), timeout=self.service.config.worker.heartbeat_seconds
                    )
                    return
                except TimeoutError:
                    if not self.service.database.renew_job_lease(
                        job.id,
                        self.worker_id,
                        lease_seconds=self.service.config.worker.lease_seconds,
                    ):
                        return

        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            return await self.service.process_claimed_job(job)
        finally:
            stop.set()
            await heartbeat_task

    def run_due_maintenance(self, now: datetime | None = None) -> dict | None:
        """Catch up the weekly Sunday 03:00 maintenance run after downtime."""
        current = (now or datetime.now(UTC)).astimezone(ZoneInfo(self.service.config.timezone))
        days_since_sunday = (current.weekday() + 1) % 7
        scheduled = (current - timedelta(days=days_since_sunday)).replace(
            hour=3, minute=0, second=0, microsecond=0
        )
        if scheduled > current:
            scheduled -= timedelta(days=7)
        last_run = self.service.database.last_maintenance_at("weekly")
        if last_run is None or last_run < scheduled.astimezone(UTC):
            return self.service.run_maintenance(apply=True)
        return None
