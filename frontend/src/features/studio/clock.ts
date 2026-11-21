/** Clock-time helpers for the long-video range picker. All values are seconds. */

export function formatClock(seconds: number): string {
  const total = Math.max(0, Math.round(seconds))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

/** "1:02:03", "62:03", "3723", "1h2m3s" -> seconds; null when it is not a time. */
export function parseClock(input: string): number | null {
  const text = input.trim().toLowerCase()
  if (!text) return null
  const units = /^(?:(\d+(?:\.\d+)?)h)?\s*(?:(\d+(?:\.\d+)?)m)?\s*(?:(\d+(?:\.\d+)?)s)?$/.exec(text)
  if (units && (units[1] || units[2] || units[3])) {
    return Number(units[1] || 0) * 3600 + Number(units[2] || 0) * 60 + Number(units[3] || 0)
  }
  const parts = text.split(':')
  if (parts.length > 3 || parts.some(part => !/^\d+(\.\d+)?$/.test(part))) return null
  return parts.reduce((total, part) => total * 60 + Number(part), 0)
}

export const MIN_RANGE_SEC = 10
export const MAX_RANGE_SEC = 60 * 60
export const MAX_RANGES = 20

export interface PickedRange { id: string; start: number; end: number; label: string }

export interface RangeProblem { key: string; values?: Record<string, number> }

/** Why `next` cannot be added next to `existing` (null when it can). `skipId` ignores the range being edited. */
export function rangeProblem(next: { start: number; end: number }, existing: PickedRange[], duration: number | null, skipId?: string): RangeProblem | null {
  const length = next.end - next.start
  if (!(next.start >= 0) || !(next.end > next.start)) return { key: 'The end time must be after the start time.' }
  if (duration != null && next.end > duration + 1) return { key: 'The range ends after the video does.' }
  if (length < MIN_RANGE_SEC) return { key: 'A range must be at least {{seconds}} seconds long.', values: { seconds: MIN_RANGE_SEC } }
  if (length > MAX_RANGE_SEC) return { key: 'A range can be at most {{minutes}} minutes long.', values: { minutes: MAX_RANGE_SEC / 60 } }
  const others = existing.filter(range => range.id !== skipId)
  if (!skipId && others.length >= MAX_RANGES) return { key: 'You can pick up to {{count}} ranges at once.', values: { count: MAX_RANGES } }
  if (others.some(range => next.start < range.end && next.end > range.start)) return { key: 'This range overlaps one you already picked.' }
  return null
}
