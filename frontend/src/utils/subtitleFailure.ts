/** 没有字幕可分析：前端用来决定要不要打开「设置 → 转写」，以及换哪一句说明。 */
export type SubtitleFailureKind =
  | 'whisper_not_installed'
  | 'whisper_install_failed'
  | 'transcription_empty'
  | 'subtitle_setup'
  | 'subtitle_unknown'

const CODES = new Set<SubtitleFailureKind>([
  'whisper_not_installed',
  'whisper_install_failed',
  'transcription_empty',
  'subtitle_setup',
  'subtitle_unknown',
])

function mentionsSubtitle(text: string): boolean {
  return /字幕|转写|Whisper|whisper|subtitle|transcri/i.test(text)
}

/**
 * 有 error_code 时以它为准。旧版只存了一句「没有字幕可分析…本地转写没有生成结果」，
 * 那种情况返回 subtitle_unknown，详情页可以再问一次当前 Whisper 状态。
 */
export function classifySubtitleFailure(error?: string | null, code?: string | null): SubtitleFailureKind | null {
  if (code && CODES.has(code as SubtitleFailureKind)) return code as SubtitleFailureKind
  // 源视频太短 / 没有音轨是素材本身的问题，转写设置帮不上忙（RC156 #10 #11）。
  if (code === 'source_too_short' || code === 'source_no_audio' || code === 'source_unreadable') return null
  const text = error || ''
  // 时间线为空会提到字幕轴，但下一步不是去装 Whisper（#182）。
  if (code === 'timeline_empty' || /时间线提取为空|时间线为空|timeline extraction is empty/i.test(text)) return null
  if (!text || !mentionsSubtitle(text)) return null
  if (/上次安装没有成功|安装没有成功|installation did not succeed|install(ation)? (failed|did not succeed)/i.test(text)) return 'whisper_install_failed'
  if (/还没安装|运行时未安装|没有可用的语音识别|not installed yet|runtime is not installed|no speech recognition service/i.test(text)) {
    return 'whisper_not_installed'
  }
  if (/没有生成可用字幕|未识别出任何语音|没有从这段视频|did not produce usable subtitles|no speech (was )?recogni[sz]ed/i.test(text)) {
    return 'transcription_empty'
  }
  if (text.includes('本地转写没有生成结果')) return 'subtitle_unknown'
  // Cloud transcription uses the same settings path, but it is not a local Whisper install.
  if (text.includes('云端转写') || /cloud transcription/i.test(text)) return null
  if (/没有字幕可分析|设置 → 语音识别|设置 → 转写|设置 → 默认模型 → 语音转字幕|no subtitles to analy[sz]e|Settings → (Speech recognition|Transcription|Default models → Speech to subtitles)/i.test(text)) {
    return 'subtitle_setup'
  }
  // 导入关卡旧文案只有「字幕文件不存在」（#186）。没有 Whisper 状态，详情页再问一次当前安装情况。
  if (text.includes('字幕文件不存在') || /subtitle file (does not exist|not found)/i.test(text)) return 'subtitle_unknown'
  return null
}
