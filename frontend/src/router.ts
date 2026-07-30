import { createRouter, createWebHashHistory } from 'vue-router'
import SitesView from './views/SitesView.vue'
import CertificatesView from './views/CertificatesView.vue'
import NodesView from './views/NodesView.vue'
import LogsView from './views/LogsView.vue'
import MonitoringView from './views/MonitoringView.vue'
import RecordsView from './views/RecordsView.vue'

export default createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/sites' },
    { path: '/sites', name: 'sites', component: SitesView },
    { path: '/certificates', name: 'certificates', component: CertificatesView },
    { path: '/nodes', name: 'nodes', component: NodesView },
    { path: '/logs', name: 'logs', component: LogsView },
    { path: '/monitoring', name: 'monitoring', component: MonitoringView },
    { path: '/records', name: 'records', component: RecordsView },
  ],
})
