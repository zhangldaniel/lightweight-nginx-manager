export type LogPreset = 'all' | 'error' | 'warn' | 'http4xx' | 'http5xx'

function statusCode(value: unknown) {
  const text = typeof value === 'number' ? String(value) : typeof value === 'string' ? value : ''
  return /^[1-5]\d\d$/.test(text) ? text : ''
}

function structuredStatus(line: string) {
  const trimmed = line.trim()
  if (trimmed.startsWith('{')) {
    try {
      const record = JSON.parse(trimmed) as Record<string, unknown>
      const response = record.response as Record<string, unknown> | undefined
      const http = record.http as Record<string, unknown> | undefined
      for (const candidate of [
        record.status,
        record.status_code,
        record.http_status,
        response?.status,
        response?.status_code,
        http?.status,
        http?.status_code,
      ]) {
        const value = statusCode(candidate)
        if (value) return value
      }
    } catch {
      // Fall through to logfmt/common-access matching for partially written lines.
    }
  }

  const named = line.match(
    /(?:^|[\s,{])["']?(?:status|status_code|http_status)["']?\s*[:=]\s*["']?([1-5]\d\d)["']?(?=$|[\s,}])/i,
  )
  if (named) return named[1]

  const common = line.match(/"[^"\r\n]*"\s+([1-5]\d\d)(?=\s|$)/)
  return common?.[1] || ''
}

function levelMatches(line: string, level: 'error' | 'warn') {
  const aliases = level === 'warn' ? 'warn(?:ing)?' : 'error'
  return new RegExp(
    `(?:\\[${aliases}\\]|\\b${aliases}\\b|["']?(?:level|severity)["']?\\s*[:=]\\s*["']?${aliases}\\b)`,
    'i',
  ).test(line)
}

export function logLineMatches(
  line: string,
  preset: LogPreset,
  mustInclude: string,
  mustExclude: string,
  caseSensitive: boolean,
) {
  const include = mustInclude.trim()
  const exclude = mustExclude.trim()
  const haystack = caseSensitive ? line : line.toLowerCase()
  const includeNeedle = caseSensitive ? include : include.toLowerCase()
  const excludeNeedle = caseSensitive ? exclude : exclude.toLowerCase()

  if (includeNeedle && !haystack.includes(includeNeedle)) return false
  if (excludeNeedle && haystack.includes(excludeNeedle)) return false
  if (preset === 'error') return levelMatches(line, 'error')
  if (preset === 'warn') return levelMatches(line, 'warn')
  if (preset === 'http4xx') return structuredStatus(line).startsWith('4')
  if (preset === 'http5xx') return structuredStatus(line).startsWith('5')
  return true
}
