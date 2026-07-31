import type { CertificateRecord } from '../types'

export function domainPatternCovers(pattern: string, domain: string) {
  const expected = String(pattern || '').trim().toLowerCase().replace(/\.$/, '')
  const actual = String(domain || '').trim().toLowerCase().replace(/\.$/, '')
  if (!expected || !actual) return false
  if (expected === actual) return true
  if (!expected.startsWith('*.')) return false
  const suffix = expected.slice(2)
  return actual.endsWith(`.${suffix}`) && actual.split('.').length === suffix.split('.').length + 1
}

export function certificateCoversDomain(
  certificate: CertificateRecord | undefined,
  domain: string,
) {
  if (!certificate) return false
  const names = Array.from(
    new Set([certificate.domain, ...(certificate.domains || [])].filter(Boolean)),
  )
  return names.some((name) => domainPatternCovers(name, domain))
}
