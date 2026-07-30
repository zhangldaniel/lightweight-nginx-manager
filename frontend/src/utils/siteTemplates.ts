import { defaultSiteConfig, normalizeProxyTarget } from './config'

export type SiteTemplateKey =
  | 'http'
  | 'https'
  | 'balanced-https'
  | 'websocket'
  | 'static'
  | 'stub-status'
  | 'stream'
  | 'custom'

export interface SiteTemplateDefinition {
  key: SiteTemplateKey
  label: string
  description: string
  context: 'http' | 'stream'
  resourceType: 'site' | 'generic'
  type: string
  defaultName?: string
  defaultFilename?: string
}

export const siteTemplates: SiteTemplateDefinition[] = [
  { key: 'http', label: 'HTTP 反向代理', description: '常规 Web 与 API 转发', context: 'http', resourceType: 'site', type: 'proxy' },
  { key: 'https', label: '标准 HTTPS', description: 'TLS 终止与单上游转发', context: 'http', resourceType: 'site', type: 'proxy' },
  { key: 'balanced-https', label: '负载均衡 HTTPS', description: '多上游、权重与失败摘除', context: 'http', resourceType: 'site', type: 'proxy' },
  { key: 'websocket', label: 'WebSocket 长连接', description: 'Upgrade 连接透传', context: 'http', resourceType: 'site', type: 'proxy' },
  { key: 'static', label: '静态站点', description: '本地目录与 SPA 回退', context: 'http', resourceType: 'site', type: 'static' },
  { key: 'stub-status', label: 'Nginx Stub Status', description: '仅监听本机 18080', context: 'http', resourceType: 'generic', type: 'custom', defaultName: 'Nginx Stub Status', defaultFilename: 'nginx-status.conf' },
  { key: 'stream', label: 'Stream TCP 代理', description: '四层 TCP 上游转发', context: 'stream', resourceType: 'generic', type: 'custom', defaultName: 'Stream TCP Proxy', defaultFilename: 'stream-proxy.stream' },
  { key: 'custom', label: '空白配置', description: '从空白 HTTP Conf 开始', context: 'http', resourceType: 'generic', type: 'custom', defaultName: '自定义配置', defaultFilename: 'custom.conf' },
]

export function renderSiteTemplate(key: SiteTemplateKey, domain: string, target: string) {
  const hostname = domain.trim() || 'api.example.com'
  const upstream = normalizeProxyTarget(target || '127.0.0.1:8080')
  if (key === 'http') return defaultSiteConfig(hostname, upstream)
  if (key === 'https') return [
    'server {', '  listen 443 ssl;', `  server_name ${hostname};`, '',
    '  ssl_certificate     /apps/nginx/cert/example.com.pem;',
    '  ssl_certificate_key /apps/nginx/cert/example.com.key;',
    '  ssl_protocols TLSv1.2 TLSv1.3;', '', '  location / {',
    `    proxy_pass ${upstream};`, '    proxy_http_version 1.1;',
    '    proxy_set_header Host $host;', '    proxy_set_header X-Real-IP $remote_addr;',
    '    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;',
    '    proxy_set_header X-Forwarded-Proto $scheme;', '  }', '}',
  ].join('\n')
  if (key === 'balanced-https') return [
    'upstream webserver {', '  hash $remote_addr;',
    '  server 10.0.0.21:8080 weight=3 max_fails=3 fail_timeout=10s max_conns=200;',
    '  server 10.0.0.22:8080 max_fails=3 fail_timeout=10s;', '}', '', 'server {',
    '  listen 443 ssl;', `  server_name ${hostname};`, '',
    '  ssl_certificate     /apps/nginx/cert/example.com.pem;',
    '  ssl_certificate_key /apps/nginx/cert/example.com.key;',
    '  ssl_protocols TLSv1.2 TLSv1.3;', '', '  location / {',
    '    proxy_pass http://webserver;', '    proxy_http_version 1.1;',
    '    proxy_set_header Host $host;', '    proxy_set_header X-Real-IP $remote_addr;',
    '    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;',
    '    proxy_set_header X-Forwarded-Proto $scheme;', '  }', '}',
  ].join('\n')
  if (key === 'websocket') return [
    'server {', '  listen 443 ssl;', `  server_name ${hostname};`, '',
    '  ssl_certificate     /apps/nginx/cert/example.com.pem;',
    '  ssl_certificate_key /apps/nginx/cert/example.com.key;', '', '  location / {',
    `    proxy_pass ${upstream};`, '    proxy_http_version 1.1;',
    '    proxy_set_header Upgrade $http_upgrade;',
    '    proxy_set_header Connection "upgrade";',
    '    proxy_set_header Host $host;', '    proxy_set_header X-Real-IP $remote_addr;',
    '    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;', '  }', '}',
  ].join('\n')
  if (key === 'static') return [
    'server {', '  listen 80;', `  server_name ${hostname};`, '',
    '  root /apps/nginx/html;', '  index index.html;', '', '  location / {',
    '    try_files $uri $uri/ /index.html;', '  }', '}',
  ].join('\n')
  if (key === 'stub-status') return [
    'server {', '  listen 127.0.0.1:18080;', '  server_name localhost;',
    '  access_log off;', '', '  location = /nginx_status {', '    stub_status;',
    '    allow 127.0.0.1;', '    deny all;', '  }', '}',
  ].join('\n')
  if (key === 'stream') return [
    'upstream tcp_backend {', '  server 127.0.0.1:3306;', '}', '',
    'server {', '  listen 13306;', '  proxy_pass tcp_backend;', '}',
  ].join('\n')
  return '# 在这里填写 HTTP 上下文中的 Nginx 配置\n'
}
