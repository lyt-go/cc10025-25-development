import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import PrefetchCapacityView from '@/views/PrefetchCapacityView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    // 固定路径要先于 /:moduleKey 注册，否则会被动态路由抢先匹配。
    { path: '/prefetch_capacity', name: 'prefetch_capacity', component: PrefetchCapacityView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
