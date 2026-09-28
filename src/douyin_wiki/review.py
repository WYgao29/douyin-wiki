from __future__ import annotations

import re

from .models import ReviewIssue, TranscriptSegment

MATERIAL_PATTERN = re.compile(r"\d|元|块|折|%|[A-Za-z]{2,}")


def _deduplicate_issues(issues: list[ReviewIssue]) -> list[ReviewIssue]:
    result: list[ReviewIssue] = []
    seen: set[tuple[int, int, int | None, str]] = set()
    for issue in issues:
        key = (issue.start_ms, issue.end_ms, issue.image_index, issue.raw_text)
        if key not in seen:
            seen.add(key)
            result.append(issue)
    return result


def detect_review_issues(segments: list[TranscriptSegment]) -> list[ReviewIssue]:
    """Missing scores are unknown, not low; only real low scores create issues."""
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


def transcript_confidence_info(segments: list[TranscriptSegment]) -> dict[str, str]:
    """Describe score availability without inventing a calibrated probability."""
    if not segments:
        return {}
    scored = sum(
        item.confidence is not None or item.avg_logprob is not None for item in segments
    )
    if not scored:
        return {
            "confidence_status": "unavailable",
            "confidence_note": (
                "当前转录结果未提供置信度分数；不代表识别质量低，不单独触发人工复核。"
            ),
        }
    if scored < len(segments):
        return {
            "confidence_status": "partial",
            "confidence_note": "部分字幕没有置信度分数；仅凭分数缺失不触发人工复核。",
        }
    return {
        "confidence_status": "available",
        "confidence_note": "转录包含模型分数或启发式分数，不等于经过校准的识别正确率。",
    }
