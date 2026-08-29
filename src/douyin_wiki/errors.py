from __future__ import annotations


class DouyinWikiError(Exception):
    """Base error carrying a stable machine-readable code."""

    code = "douyin_wiki_error"

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class InvalidShareTextError(DouyinWikiError):
    code = "invalid_share_text"


class CookieRequiredError(DouyinWikiError):
    code = "cookie_required"


class VideoUnavailableError(DouyinWikiError):
    code = "video_unavailable"


class VideoTooLongError(DouyinWikiError):
    code = "video_too_long"


class RegionRestrictedError(DouyinWikiError):
    code = "region_restricted"


class BrowserAuthRequiredError(DouyinWikiError):
    code = "browser_auth_required"


class LivePhotoUnsupportedError(DouyinWikiError):
    code = "live_photo_unsupported"


class ExternalToolError(DouyinWikiError):
    code = "external_tool_error"


class ModelConfigurationError(DouyinWikiError):
    code = "model_not_configured"


class JobStateError(DouyinWikiError):
    code = "invalid_job_state"


class JobLeaseLostError(JobStateError):
    code = "job_lease_lost"


class EntryNotFoundError(DouyinWikiError):
    code = "entry_not_found"
