"""
流水线「明确失败」异常。

原则：LLM 不可用、字幕缺失、大纲 / 评分为空、切片没产出文件——这些都必须让项目进 failed 态，
带上阶段和一句用户能照着做的提示；不能吞掉后继续跑成 `Completed · 0 切片`（#100 #11 #24 的共同表象）。
"""

from __future__ import annotations


class PipelineFailure(RuntimeError):
    """带阶段与用户提示的流水线失败。

    stage: 与 simple_progress 的阶段名一致（INGEST / SUBTITLE / ANALYZE / HIGHLIGHT / EXPORT），
           前端失败态与应用内反馈会带上它。
    hint:  用户下一步能做什么（去哪个设置项、装什么），追加在错误正文后面。
    """

    def __init__(
        self,
        stage: str,
        message: str,
        hint: str = "",
        code: str = "",
        http_status: int | None = None,
        upstream_error_type: str = "",
    ):
        super().__init__(message)
        self.stage = stage
        self.hint = hint
        # 上游 HTTP 状态码（仅 400–599 的整数）；没有链上原始异常时（如时间线汇总失败）用它带出状态码。
        self.http_status = http_status
        # 稳定机器码。前端用来打开对应设置页，或避免打开错误的设置页。
        # llm_not_configured：没有可用提供商 / 缺少 API Key
        # llm_auth_invalid：上游拒绝密钥或权限（401/403）
        # whisper_not_installed | whisper_install_failed | transcription_empty | subtitle_setup
        # timeline_empty：有效候选被时长筛选清空（不要再指到「设置 → 服务商与模型」）
        # source_too_short | source_no_audio：源视频本身不能走字幕路线（media_precheck），用户输入，不上报
        self.code = code
        # Fine-grained upstream kind (insufficient_quota / invalid_key / …). Not a studio
        # error_code — keep analytics on the coarse `code` until the #9 contract expands.
        self.upstream_error_type = upstream_error_type

    @property
    def message(self) -> str:
        return str(self.args[0]) if self.args else ""

    def user_message(self) -> str:
        return f"{self.message} {self.hint}".strip()


HINT_CHECK_LLM = 'Go to "Settings → Providers & models", check the provider, API key and model name, click "Test connection" to confirm, then try again.'
# 缺密钥 / 提供商不可用 / 连接测试失败：明确要自备 key，并指到设置里的「模型」。
HINT_BRING_OWN_KEY = (
    "You need to bring your own API key (for local Ollama / LM Studio, start the service first). "
    'Go to "Settings → Providers & models", fill in the provider, key and model name, click "Test connection", then try again. '
    "Get the key from the provider's console; it is stored only on this machine."
)
CODE_LLM_NOT_CONFIGURED = "llm_not_configured"
def _model_call_messages() -> dict[str, str]:
    from backend.core.failure_codes import sentence
    return {code: sentence(code) for code in ("llm_auth_invalid", "connection", "timeout", "rate_limited")}
MODEL_CALL_MESSAGES = _model_call_messages()


def looks_like_llm_setup_error(text: str) -> bool:
    """调用模型失败，是因为没有可用密钥 / 连接测试没通过，而不是内容本身。"""
    raw = (text or "").lower()
    needles = (
        "没有可用的 llm",
        "缺少 api key",
        "未配置llm",
        "no llm provider configured",
        "未配置 api",
        "api key为空",
        "api key 为空",
        "invalid api key",
        "incorrect api key",
        "api连接测试失败",
        "连接测试失败",
        "authentication",
        "unauthorized",
        "missing api key",
        "no api key",
    )
    return any(n in raw for n in needles)


def llm_key_failure(stage: str, message: str) -> PipelineFailure:
    return PipelineFailure(stage, message, HINT_BRING_OWN_KEY, code=CODE_LLM_NOT_CONFIGURED)


HINT_SUBTITLE = 'Go to "Settings → Default models → Speech to subtitles" to install a Whisper model so AutoClip can transcribe automatically, or import an .srt subtitle file and try again.'
HINT_LOWER_THRESHOLD = 'Go to "Settings → Providers & models → Minimum score" to lower it and try again, or switch to a stronger model.'
HINT_CHECK_FFMPEG = "Make sure ffmpeg is available (bundled in the desktop app; in Docker / script mode check PATH) and the source video file is complete and playable."
# Legacy fallback when a caller returns [] without Step 2 diagnostics.
# The extractor itself distinguishes request, response and duration failures.
CODE_TIMELINE_EMPTY = "timeline_empty"
HINT_EMPTY_TIMELINE = (
    "Clips in short videos are often dropped by the minimum duration (short clips start at about 20 seconds), or the timestamps from the model do not match the subtitles. "
    "Try again with longer material that has more complete narration."
)


def empty_timeline_failure(topic_count: int) -> PipelineFailure:
    """旧调用方只返回空列表时的兜底。正常 Step 2 使用本轮报告分类。"""
    return PipelineFailure(
        "ANALYZE",
        f"Timeline extraction is empty: {topic_count} topics left no usable segments after alignment and duration filtering.",
        HINT_EMPTY_TIMELINE,
        code=CODE_TIMELINE_EMPTY,
    )


def model_call_error_code(error: Exception) -> str:
    """Coarse studio code only. Fine type lives on ClassifiedUpstreamError / PipelineFailure.upstream_error_type."""
    from backend.core.failure_attribution import frozen_error_code
    from backend.core.llm_retry import studio_code_for
    from backend.core.upstream_errors import classify_exception, http_error_parts

    frozen = frozen_error_code(error)  # the redaction boundary already decided it
    if frozen:
        return frozen
    info = classify_exception(error)
    status, _body = http_error_parts(error)
    code = studio_code_for(info.error_type, status)
    if code == "provider_error" and looks_like_llm_setup_error(str(error)):
        return "authentication"
    return code


def http_status_of(error: BaseException | None) -> int | None:
    """第一个 400–599 的整数 HTTP 状态码，不读正文、URL 或密钥。

    （ ，和 同一套遍历规则）：只有 failure_attribution.http_status_of 一处实现。
    只走显式 __cause__；脱敏边界冻结在安全异常上的 http_status 优先；不再走 __context__
    （处理旧 503 时又发生的新错误，不能把旧状态码带进新请求）。
    """
    from backend.core.failure_attribution import http_status_of as _one_rule
    return _one_rule(error)


def timeline_failure_from_report(topic_count: int, report: dict) -> PipelineFailure:
    """Use current-run observations, not a guess that an empty timeline is short."""
    chunks = report.get("chunks", [])
    if report.get("parsed", 0):
        minimum = report.get("minimum_seconds")
        if minimum is not None:
            return PipelineFailure(
                "ANALYZE",
                f"Timeline extraction is empty: {report['parsed']} valid candidates all fell short of the minimum duration ({minimum:g} s) after subtitle boundary correction.",
                "Still too short after merging and padding adjacent segments. Use material with continuous narration that forms complete topics; lowering the score threshold does not change the duration limit.",
                code=CODE_TIMELINE_EMPTY,
            )
        return empty_timeline_failure(topic_count)
    failed_calls = [chunk for chunk in chunks if chunk.get("outcome") == "call_failed"]
    if failed_calls:
        from backend.core.upstream_errors import hint_key_for

        last = failed_calls[-1]
        code = last["error_code"]
        kind = last.get("upstream_error_type") or ""
        hints = {
            "authentication": 'Check the key, model permissions and provider settings in "Settings → Providers & models", then test the connection.',
            "rate_limited": MODEL_CALL_MESSAGES["rate_limited"],
            "timeout": MODEL_CALL_MESSAGES["timeout"],
            "connection": MODEL_CALL_MESSAGES["connection"],
            "provider_error": "The provider did not complete the timeline request. Check model availability and API compatibility, then try again.",
        }
        classified = hint_key_for(kind) if kind else hints.get(code, hints["provider_error"])
        return PipelineFailure(
            "ANALYZE", f"Timeline model call failed: {len(failed_calls)}/{len(chunks)} subtitle chunk calls failed, so there are no usable candidates this time.",
            classified + " A successful outline or connection test does not guarantee that later long-text requests will succeed.", code=code,
            http_status=last.get("http_status"), upstream_error_type=kind,
        )
    if chunks and all(chunk.get("outcome") == "missing_subtitles" for chunk in chunks):
        return PipelineFailure("ANALYZE", "The subtitle chunks needed for the timeline could not be read.",
                               "Re-import the source video and its matching SRT; keep the original project for troubleshooting.", code="missing_resource")
    if any(chunk.get("outcome") == "chunk_error" for chunk in chunks):
        return PipelineFailure("ANALYZE", "Timeline chunk processing failed, so there are no usable candidates this time.",
                               "Make sure the project data directory is readable and writable and keep the failed-stage feedback; this is not a score threshold issue.", code="unexpected")
    if chunks:
        rejected = {}
        for chunk in chunks:
            for reason, count in chunk.get("rejected", {}).items():
                rejected[reason] = rejected.get(reason, 0) + count
        invalid_ranges = rejected.get("invalid_time", 0) + rejected.get("outside_chunk_or_reversed", 0)
        detail = (f"{invalid_ranges} of the candidates have invalid or reversed timestamps, or fall outside the current subtitle chunk." if invalid_ranges else "")
        from backend.core.weak_local_timeline import WEAK_LOCAL_TIMELINE_REASON, empty_interval_hint

        cached = any(isinstance(chunk, dict) and chunk.get("source") == "cache" for chunk in chunks)
        if report.get("weak_local_timeline") and not cached:
            return PipelineFailure("ANALYZE", WEAK_LOCAL_TIMELINE_REASON, "", code="invalid_response")
        return PipelineFailure(
            "ANALYZE", f"The timeline model reply has no valid time ranges ({topic_count} topics, {len(chunks)} subtitle chunks). {detail}",
            empty_interval_hint(report, chunks) + " This step has not yet reached scoring or minimum duration filtering.", code="invalid_response",
        )
    return empty_timeline_failure(topic_count)


from backend.core.failure_attribution import AttributedError  # noqa: E402 - no cycle


class AttributedPipelineFailure(AttributedError, PipelineFailure):
    """a PipelineFailure carrying the snapshot built where the chain was intact (``.attribution``)."""


def cloud_transcription_failure(message, error: BaseException | None = None, http_status: int | None = None,
                                upstream_error_type: str = "") -> PipelineFailure:
    """CloudTranscriptionError carries only our safe public text, never a provider body.

    the transcription boundary already built the snapshot (``error.attribution``); it is carried
    here as is and wins over this wrapper's code (which only picks the user hint), never re-judged.
    the code is the one the boundary froze (timeout / connection / llm_auth_invalid /...),
    else ``provider_error``. The fine user cause (``invalid_key`` / ``insufficient_quota``) stays on
    ``upstream_error_type``. The user text depends ONLY on the code and the frozen HTTP status.
    A non-str ``message`` is the safe error itself (callers pass the exception)."""
    from backend.core.failure_attribution import attributed, frozen_error_code
    if not isinstance(message, str):
        error = error or message
        message = str(message)
    if error is not None:
        http_status = http_status or getattr(error, "http_status", None)
        upstream_error_type = upstream_error_type or getattr(error, "upstream_error_type", "") or ""
    if upstream_error_type == "unknown":
        upstream_error_type = ""
    code = frozen_error_code(error) or "provider_error"
    failure = AttributedPipelineFailure(
        "SUBTITLE", f"Cloud transcription failed: {message}",
        'Go to "Settings → Default models → Speech to subtitles" and check the selected cloud transcription service, key and model; once the service is available, try again or import an .srt subtitle file.',
        code=code, http_status=http_status if isinstance(http_status, int) else None,
        upstream_error_type=upstream_error_type,
    )
    return attributed(failure, getattr(error, "attribution", None), http_status=http_status,
                      error_code=code if code != "provider_error" else frozen_error_code(error))


def transcription_not_ready_failure(check: dict, stage: str = "SUBTITLE") -> PipelineFailure:
    """ASR is not ready: same codes the frontend import gate already uses, before any paid call."""
    code = (check or {}).get("code") or "whisper_not_installed"
    if code in {"whisper_install_failed", "whisper_model_failed"}:
        return PipelineFailure(
            stage,
            "No subtitles to analyze: the video has no subtitles and the last Whisper installation did not succeed.",
            'Go to "Settings → Default models → Speech to subtitles" to retry the installation, or re-import with an .srt subtitle file.',
            code="whisper_install_failed",
        )
    if code == "sensevoice_not_ready":
        return PipelineFailure(
            stage,
            "No subtitles to analyze: SenseVoice did not produce usable subtitles this time.",
            'Go to "Settings → Default models → Speech to subtitles" and check that SenseVoiceSmall is ready, or import an .srt subtitle file and try again.',
            code="subtitle_setup",
        )
    if code == "cloud_not_configured":
        return PipelineFailure(
            stage,
            "Cloud transcription failed: no usable transcription result was returned.",
            'Go to "Settings → Default models → Speech to subtitles" and check the selected cloud transcription service, key and model; once the service is available, try again or import an .srt subtitle file.',
            code="provider_error",
        )
    return PipelineFailure(
        stage,
        "No subtitles to analyze: the video has no subtitles and local Whisper is not installed yet.",
        'Go to "Settings → Default models → Speech to subtitles" to install Whisper and download a model, or re-import with an .srt subtitle file.',
        code="whisper_not_installed",
    )


def missing_subtitle_failure() -> PipelineFailure:
    """视频没有字幕，自动转写也没留下 srt。按所选转写服务区分下一步。"""
    from backend.services import whisper_runtime
    from backend.services.ai_model_settings import load
    settings = load()
    if settings and settings.transcription and settings.transcription.provider == 'cloud':
        return cloud_transcription_failure("No usable transcription result was returned.")
    if settings and settings.transcription and settings.transcription.provider == 'sensevoice_local':
        return PipelineFailure('SUBTITLE', 'No subtitles to analyze: SenseVoice did not produce usable subtitles this time.',
                               'Go to "Settings → Default models → Speech to subtitles" and check that SenseVoiceSmall is ready, or import an .srt subtitle file and try again.',
                               code='subtitle_setup')

    status = whisper_runtime.get_status()
    state = status.get("status")
    if state == "error":
        return PipelineFailure(
            "SUBTITLE",
            "No subtitles to analyze: the video has no subtitles and the last Whisper installation did not succeed.",
            'Go to "Settings → Default models → Speech to subtitles" to retry the installation, or re-import with an .srt subtitle file.',
            code="whisper_install_failed",
        )
    if state != "installed":
        return PipelineFailure(
            "SUBTITLE",
            "No subtitles to analyze: the video has no subtitles and local Whisper is not installed yet.",
            'Go to "Settings → Default models → Speech to subtitles" to install Whisper and download a model, or re-import with an .srt subtitle file.',
            code="whisper_not_installed",
        )
    return PipelineFailure(
        "SUBTITLE",
        "No subtitles to analyze: the video has no subtitles and Whisper is installed, but this transcription did not produce usable subtitles.",
        "Make sure the video has clear speech and try again, or re-import with an .srt subtitle file.",
        code="transcription_empty",
    )


def failure_from_speech_error(message: str) -> PipelineFailure:
    """转写异常收成同一套失败码，不再把「设置 → 语音识别」和「设置 → 默认模型 → 语音转字幕」叠在一起。"""
    text = (message or "").strip().replace("设置 → 语音识别", "设置 → 默认模型 → 语音转字幕").replace("设置 → 转写", "设置 → 默认模型 → 语音转字幕")
    from backend.pipeline import media_precheck

    if media_precheck.is_no_audio_text(text):
        # 视频本身没有音轨：不是转写设置的问题，不要指到「设置 → 默认模型 → 语音转字幕」。
        return media_precheck.no_audio_failure("SUBTITLE")
    if "已超过时间上限" in text:
        # Local Whisper wall clock (C2), same studio code as the clip-job cap.
        return PipelineFailure("SUBTITLE", text, code="timeout")
    if 'SenseVoice' in text:
        return PipelineFailure('SUBTITLE', text,
                               '' if ('设置 → 默认模型 → 语音转字幕' in text or 'Settings → Default models → Speech to subtitles' in text) else 'Go to "Settings → Default models → Speech to subtitles" and check SenseVoiceSmall, or import an .srt subtitle file.',
                               code='subtitle_setup')
    from backend.services import whisper_runtime

    state = whisper_runtime.get_status().get("status")
    not_installed = ("未安装" in text) or ("没有可用的语音识别" in text)
    if not_installed:
        if state == "error":
            return missing_subtitle_failure()
        return PipelineFailure(
            "SUBTITLE",
            "No subtitles to analyze: the video has no subtitles and local Whisper is not installed yet.",
            'Go to "Settings → Default models → Speech to subtitles" to install Whisper and download a model, or re-import with an .srt subtitle file.',
            code="whisper_not_installed",
        )
    if any(token in text for token in ("未识别出任何语音", "没有从这段视频", "可用语音")):
        return PipelineFailure(
            "SUBTITLE",
            "No subtitles to analyze: the video has no subtitles and Whisper is installed, but this transcription did not produce usable subtitles.",
            "Make sure the video has clear speech and try again, or re-import with an .srt subtitle file.",
            code="transcription_empty",
        )
    hint = "" if ("设置 → 默认模型 → 语音转字幕" in text or "Settings → Default models → Speech to subtitles" in text) else HINT_SUBTITLE
    return PipelineFailure("SUBTITLE", text, hint, code="subtitle_setup")
