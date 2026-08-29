from __future__ import annotations

import json
import subprocess

from ..errors import ExternalToolError, JobStateError
from ..models import ReminderCandidate

JXA_SCRIPT = r"""
function run(argv) {
  const title = argv[0];
  const dueAt = argv[1];
  const notes = argv[2];
  const marker = argv[3];
  const app = Application('Reminders');
  app.includeStandardAdditions = true;
  const lists = app.lists();
  if (!lists.length) throw new Error('No reminder list is available');
  for (const list of lists) {
    for (const existing of list.reminders()) {
      const body = existing.body() || '';
      if (body.includes(marker)) {
        return JSON.stringify({id: existing.id(), name: existing.name(), existing: true});
      }
    }
  }
  const reminder = app.Reminder({name: title, body: notes, dueDate: new Date(dueAt)});
  lists[0].reminders.push(reminder);
  return JSON.stringify({id: reminder.id(), name: reminder.name()});
}
"""


class MacOSReminderAdapter:
    def create(
        self, candidate: ReminderCandidate, *, source_url: str, idempotency_key: str | None = None
    ) -> str:
        if candidate.due_at is None or candidate.needs_clarification:
            raise JobStateError("提醒时间不明确，必须先由用户确认绝对时间")
        marker = f"douyin-wiki:{idempotency_key or candidate.id}"
        notes = (
            f"来源：{source_url}\n原因：{candidate.reason}\n原话：{candidate.source_quote}"
            f"\n\n[{marker}]"
        )
        try:
            result = subprocess.run(
                [
                    "osascript",
                    "-l",
                    "JavaScript",
                    "-e",
                    JXA_SCRIPT,
                    "--",
                    candidate.title,
                    candidate.due_at.isoformat(),
                    notes,
                    marker,
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise ExternalToolError("无法调用 macOS 提醒事项") from exc
        if result.returncode != 0:
            raise ExternalToolError(
                "创建 macOS 提醒失败；请检查自动化权限",
                details={"stderr": result.stderr.strip()},
            )
        try:
            return str(json.loads(result.stdout.strip())["id"])
        except (json.JSONDecodeError, KeyError) as exc:
            raise ExternalToolError("提醒事项已调用，但未返回系统 ID") from exc
