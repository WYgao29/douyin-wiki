from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .errors import JobLeaseLostError
from .runtime import LOADED_SOURCE_SIGNATURE, source_signature
from .service import DouyinWikiService


class Worker:
    def __init__(
        self,
        service: DouyinWikiService,
        *,
        loaded_signature: str = LOADED_SOURCE_SIGNATURE,
        signature_provider: Callable[[], str] = source_signature,
    ) -> None:
        self.service = service
        self.worker_id = f"worker-{uuid.uuid4().hex}"
        self.loaded_signature = loaded_signature
        self.signature_provider = signature_provider
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
        try:
            self.run_due_maintenance()
        except Exception as exc:  # Maintenance must not block capture queue availability.
            sys.stderr.write(
                f"抖库补偿维护执行失败，Worker 将继续处理采集任务：{type(exc).__name__}: {exc}\n"
            )
        running: set[asyncio.Task] = set()
        while True:
            completed = {task for task in running if task.done()}
            for task in completed:
                try:
                    task.result()
                except Exception as exc:  # A single unexpected task must not stop the daemon.
                    sys.stderr.write(
                        f"抖库任务收尾失败，Worker 将继续运行：{type(exc).__name__}: {exc}\n"
                    )
            running -= completed
            if self.source_changed():
                if running:
                    await asyncio.wait(running, timeout=self.service.config.worker.poll_seconds)
                    continue
                sys.stderr.write("抖库代码已更新，Worker 正在退出并由 LaunchAgent 重启。\n")
                return
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

    def source_changed(self) -> bool:
        """Stop an installed daemon before it can claim jobs with stale code."""
        try:
            return self.signature_provider() != self.loaded_signature
        except OSError:
            return False

    async def _process_with_heartbeat(self, job):
        stop = asyncio.Event()
        lease_lost = asyncio.Event()

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
                        lease_lost.set()
                        return

        heartbeat_task = asyncio.create_task(heartbeat())
        async def process():
            with self.service.database.claimed_job_updates(job.id, self.worker_id):
                return await self.service.process_claimed_job(job)

        processing_task = asyncio.create_task(process())
        lease_lost_task = asyncio.create_task(lease_lost.wait())
        try:
            done, _ = await asyncio.wait(
                {processing_task, lease_lost_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if processing_task in done:
                return processing_task.result()
            processing_task.cancel()
            with suppress(asyncio.CancelledError):
                await processing_task
            raise JobLeaseLostError(f"任务 {job.id} 的 Worker 租约已失效")
        finally:
            stop.set()
            lease_lost_task.cancel()
            with suppress(asyncio.CancelledError):
                await lease_lost_task
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
