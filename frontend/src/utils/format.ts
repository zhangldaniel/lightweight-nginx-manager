import type { CertificateRecord, SiteRecord, Tone } from '../types'

export function relativeTime(value?: string | null) {
  if (!value) return '从未'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const seconds = Math.max(0, Math.round((Date.now() - date.getTime()) / 1000))
  if (seconds < 10) return '刚刚'
  if (seconds < 60) return `${seconds} 秒前`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  const days = Math.floor(hours / 24)
  if (days < 30) return `${days} 天前`
  return date.toLocaleString('zh-CN', { hour12: false })
}

export function dateTime(value?: string | null) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}

export function siteTitle(site: SiteRecord) {
  return site.resourceType === 'generic'
    ? site.name || site.filename || '通用 Conf'
    : site.domain || site.name || '未命名站点'
}

export function siteKind(site: SiteRecord) {
  if (site.context === 'main') return '主配置'
  if (site.context === 'stream') return 'Stream'
  if (site.resourceType === 'generic') return '通用 Conf'
  if (site.type === 'static') return '静态站点'
  return '反向代理'
}

export function siteStatus(site: SiteRecord): { label: string; tone: Tone } {
  if (site.pendingRemote) return { label: '执行中', tone: 'info' }
  const status = site.status
  if (status === 'published') return { label: '已发布', tone: 'success' }
  if (status === 'draft') return { label: '有草稿', tone: 'info' }
  if (status === 'unassigned') return { label: '未部署', tone: 'neutral' }
  if (status === 'drift') return { label: '配置漂移', tone: 'warning' }
  if (status === 'failed') return { label: '发布失败', tone: 'danger' }
  if (status === 'publishing') return { label: '发布中', tone: 'info' }
  return { label: status || '未知', tone: 'neutral' }
}

export function certificateDays(certificate: CertificateRecord) {
  if (Number.isFinite(Number(certificate.daysLeft))) return Number(certificate.daysLeft)
  if (!certificate.expiresAt) return null
  const value = Math.ceil((new Date(certificate.expiresAt).getTime() - Date.now()) / 86_400_000)
  return Number.isFinite(value) ? value : null
}

export function certificateStatus(certificate: CertificateRecord): { label: string; tone: Tone } {
  if (certificate.pendingRemote || certificate.status === 'replacing') {
    return { label: '替换中', tone: 'info' }
  }
  if (certificate.status === 'draft') return { label: '待部署', tone: 'info' }
  if (certificate.status === 'failed') return { label: '替换失败', tone: 'danger' }
  const days = certificateDays(certificate)
  if (days !== null && days < 0) return { label: '已过期', tone: 'danger' }
  if (days !== null && days <= 7) return { label: `${days} 天后到期`, tone: 'danger' }
  if (days !== null && days <= 30) return { label: `${days} 天后到期`, tone: 'warning' }
  return { label: '正常', tone: 'success' }
}

export function bytes(value: unknown) {
  const number = Number(value)
  if (!Number.isFinite(number)) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let amount = Math.max(0, number)
  let index = 0
  while (amount >= 1024 && index < units.length - 1) {
    amount /= 1024
    index += 1
  }
  return `${amount >= 100 || index === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[index]}`
}

export function metric(metrics: Record<string, unknown>, path: string, fallback = 0) {
  let value: unknown = metrics
  for (const part of path.split('.')) {
    if (!value || typeof value !== 'object') return fallback
    value = (value as Record<string, unknown>)[part]
  }
  return typeof value === 'number' ? value : fallback
}
