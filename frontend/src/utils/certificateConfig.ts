import type { CertificatePath, CertificateRecord, NodeRecord } from '../types'

export interface CertificateRewriteResult {
  content: string
  replacements: number
}

export interface CertificateDirectiveCounts {
  certificate: number
  key: number
}

interface ValueRange {
  start: number
  end: number
}

export function certificatePathsForNode(
  certificate: CertificateRecord | undefined,
  node: NodeRecord | undefined,
): CertificatePath | null {
  if (!certificate || !node) return null
  const paths = certificate.nodePaths?.[node.id]
  const certificatePath = String(paths?.certificatePath || '').trim()
  const keyPath = String(paths?.keyPath || '').trim()
  if (!certificatePath || !keyPath) return null
  return { certificatePath, keyPath }
}

function directiveValueRanges(content: string, directive: string): ValueRange[] {
  const ranges: ValueRange[] = []
  let index = 0
  let statementStart = true
  let quote = ''
  let escaped = false

  while (index < content.length) {
    const character = content[index]
    if (quote) {
      if (escaped) escaped = false
      else if (character === '\\') escaped = true
      else if (character === quote) quote = ''
      index += 1
      continue
    }
    if (character === '"' || character === "'") {
      quote = character
      index += 1
      continue
    }
    if (character === '#') {
      while (index < content.length && content[index] !== '\n') index += 1
      continue
    }
    if (/\s/.test(character)) {
      index += 1
      continue
    }
    if (character === '{' || character === '}' || character === ';') {
      statementStart = true
      index += 1
      continue
    }

    const tokenStart = index
    while (index < content.length && !/[\s;{}]/.test(content[index])) index += 1
    const token = content.slice(tokenStart, index)
    if (!statementStart || token.toLowerCase() !== directive.toLowerCase()) {
      statementStart = false
      continue
    }

    while (index < content.length && /\s/.test(content[index])) index += 1
    const valueStart = index
    let valueQuote = ''
    let valueEscaped = false
    while (index < content.length) {
      const valueCharacter = content[index]
      if (valueQuote) {
        if (valueEscaped) valueEscaped = false
        else if (valueCharacter === '\\') valueEscaped = true
        else if (valueCharacter === valueQuote) valueQuote = ''
        index += 1
        continue
      }
      if (valueCharacter === '"' || valueCharacter === "'") {
        valueQuote = valueCharacter
        index += 1
        continue
      }
      if (valueCharacter === '#') {
        while (index < content.length && content[index] !== '\n') index += 1
        continue
      }
      if (valueCharacter === ';') break
      index += 1
    }
    if (index < content.length && content[index] === ';') {
      let valueEnd = index
      while (valueEnd > valueStart && /\s/.test(content[valueEnd - 1])) valueEnd -= 1
      ranges.push({ start: valueStart, end: valueEnd })
      index += 1
      statementStart = true
    } else {
      statementStart = false
    }
  }
  return ranges
}

function replaceDirective(content: string, directive: string, path: string) {
  const ranges = directiveValueRanges(content, directive)
  let rewritten = content
  for (const range of [...ranges].reverse()) {
    rewritten = `${rewritten.slice(0, range.start)}${path}${rewritten.slice(range.end)}`
  }
  return { content: rewritten, replacements: ranges.length }
}

export function certificateDirectiveCounts(content: string): CertificateDirectiveCounts {
  const source = String(content || '')
  return {
    certificate: directiveValueRanges(source, 'ssl_certificate').length,
    key: directiveValueRanges(source, 'ssl_certificate_key').length,
  }
}

export function rewriteConfigCertificatePaths(
  content: string,
  certificate: CertificateRecord | undefined,
  node: NodeRecord | undefined,
): CertificateRewriteResult {
  const paths = certificatePathsForNode(certificate, node)
  if (!paths) return { content: String(content || ''), replacements: 0 }

  const certificateResult = replaceDirective(
    String(content || ''),
    'ssl_certificate',
    paths.certificatePath,
  )
  const keyResult = replaceDirective(
    certificateResult.content,
    'ssl_certificate_key',
    paths.keyPath,
  )
  return {
    content: keyResult.content,
    replacements: certificateResult.replacements + keyResult.replacements,
  }
}

export function configForCertificateNode(
  content: string,
  certificate: CertificateRecord | undefined,
  node: NodeRecord,
) {
  if (!certificate) return String(content || '')
  if (!certificatePathsForNode(certificate, node)) {
    throw new Error(`${node.node_name} 缺少所选证书的证书路径或私钥路径`)
  }
  return rewriteConfigCertificatePaths(content, certificate, node).content
}