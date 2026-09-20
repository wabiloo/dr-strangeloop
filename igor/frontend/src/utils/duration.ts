/** Best-effort parser for franken-ts's asset `duration`/`start` time
 * formats (see franken-ts/README.md "Asset time formats"): plain seconds,
 * `HH:MM:SS[.mmm]`, and human phrases like "10 min" / "1 hour 30 min".
 * Used only for the timeline visualization's proportions -- not a
 * validating parser (franken-ts/pytimeparse does the real parsing
 * server-side); returns null when it can't make sense of the input, and
 * callers should fall back to an equal-weight placeholder in that case. */
export function parseApproxSeconds(input: string | number | undefined | null): number | null {
  if (input === undefined || input === null || input === '') return null
  if (typeof input === 'number') return input

  const s = input.trim()
  if (!s) return null

  // Plain number.
  if (/^\d+(\.\d+)?$/.test(s)) return Number(s)

  // HH:MM:SS[.mmm]
  const clock = s.match(/^(\d+):(\d{1,2}):(\d{1,2}(?:\.\d+)?)$/)
  if (clock) {
    const [, h, m, sec] = clock
    return Number(h) * 3600 + Number(m) * 60 + Number(sec)
  }

  // Human phrases: "1 hour 30 min 5 sec", "10 min", "2s"
  const unitPattern = /(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b/gi
  let total = 0
  let matched = false
  for (const match of s.matchAll(unitPattern)) {
    matched = true
    const value = Number(match[1])
    const unit = match[2].toLowerCase()
    if (unit.startsWith('h')) total += value * 3600
    else if (unit.startsWith('m')) total += value * 60
    else total += value
  }
  return matched ? total : null
}
