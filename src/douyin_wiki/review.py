from __future__ import annotations

import re

from .models import ReviewIssue, TranscriptSegment

MATERIAL_PATTERN = re.compile(r"\d|元|块|折|%|[A-Za-z]{2,}")


def detect_review_issues(segments: list[TranscriptSegment]) -> list[ReviewIssue]:
    issues: list[ReviewIssue] = []
    for segment in segments:
        low_confidence = (segment.confidence is not None and segment.confidence < 0.55) or (
            segment.avg_logprob is not None and segment.avg_logprob < -1.0
        )
        very_low_confidence = segment.confidence is not None and segment.confidence < 0.35
        material = bool(MATERIAL_PATTERN.search(segment.text))
        if low_confidence and (material or very_low_confidence):
            issues.append(
                ReviewIssue(
                    id=f"asr-{segment.id}",
                    start_ms=segment.start_ms,
                    end_ms=segment.end_ms,
                    raw_text=segment.text,
                    reason="低置信转录包含可能影响结论的人名、数字、日期或专有词",
                )
            )
    return issues


def apply_review_resolutions(
    segments: list[TranscriptSegment], issues: list[ReviewIssue]
) -> list[TranscriptSegment]:
    replacements = {
        (issue.start_ms, issue.end_ms): issue.resolution
        for issue in issues
        if issue.resolution is not None
    }
    return [
        segment.model_copy(
            update={"text": replacements.get((segment.start_ms, segment.end_ms), segment.text)}
        )
        for segment in segments
    ]
