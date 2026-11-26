<template>
  <div v-if="!store.user">
    <router-view />
  </div>
  <div v-else>
    <header class="topbar">
      <span class="brand">🔋 退役电池收货与安全放行</span>
      <nav>
        <router-link to="/receiving">收货/隔离</router-link>
        <router-link to="/dismantling">拆解/换标</router-link>
        <router-link to="/inspection">检测台</router-link>
        <router-link to="/scheduling">检验排程</router-link>
        <router-link to="/safety">安全审批</router-link>
        <router-link to="/warehouse">仓库/核对</router-link>
        <router-link to="/downstream">下游签收</router-link>
        <router-link to="/scenario">L-7 场景</router-link>
        <router-link to="/audit">审计日志</router-link>
      </nav>
      <span class="spacer"></span>
      <span class="small">{{ store.user.display_name }}（{{ roleName(store.user.role) }}）</span>
      <button class="ghost" @click="store.logout(); $router.push('/login')">退出</button>
    </header>
    <main>
      <router-view />
    </main>
  </div>
</template>

<script setup>
import { store } from './api'

const ROLE_NAMES = {
  receiver: '接收员', dismantler: '拆解员', safety: '安全工程师',
  warehouse: '仓库', downstream: '下游买家', admin: '管理员', inspector: '检验员',
}
const roleName = (r) => ROLE_NAMES[r] || r
</script>
