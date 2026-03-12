<template>
  <el-container class="app-container">
    <el-aside :width="isCollapsed ? '64px' : '200px'" class="app-aside">
      <div class="aside-header">
        <el-icon :size="28" color="#409eff"><Headset /></el-icon>
        <span v-show="!isCollapsed" class="app-title">ASR 管理器</span>
      </div>
      <el-menu
        :default-active="$route.path"
        :collapse="isCollapsed"
        router
        class="aside-menu"
        background-color="#1d1e1f"
        text-color="#bfcbd9"
        active-text-color="#409eff"
      >
        <el-menu-item index="/upload">
          <el-icon><Upload /></el-icon>
          <template #title>文件上传</template>
        </el-menu-item>
        <el-menu-item index="/tasks">
          <el-icon><List /></el-icon>
          <template #title>任务列表</template>
        </el-menu-item>
        <el-menu-item index="/monitor">
          <el-icon><Monitor /></el-icon>
          <template #title>系统监控</template>
        </el-menu-item>
      </el-menu>
      <div class="aside-footer">
        <el-button text :icon="isCollapsed ? Expand : Fold" @click="isCollapsed = !isCollapsed" class="collapse-btn" />
      </div>
    </el-aside>
    <el-container>
      <el-header class="app-header">
        <span class="header-breadcrumb">{{ currentTitle }}</span>
        <div class="header-right">
          <span class="version-tag">v{{ appVersion }}</span>
        </div>
      </el-header>
      <el-main class="app-main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { Headset, Upload, List, Monitor, Fold, Expand } from '@element-plus/icons-vue'

const appVersion = __APP_VERSION__

const route = useRoute()
const isCollapsed = ref(false)

const SMALL_SCREEN_BREAKPOINT = 768

function handleResize() {
  isCollapsed.value = window.innerWidth < SMALL_SCREEN_BREAKPOINT
}

onMounted(() => {
  handleResize()
  window.addEventListener('resize', handleResize)
})
onUnmounted(() => {
  window.removeEventListener('resize', handleResize)
})

const titleMap = {
  '/upload': '文件上传',
  '/tasks': '任务列表',
  '/monitor': '系统监控',
}
const currentTitle = computed(() => {
  if (route.path.startsWith('/tasks/')) return '任务详情'
  return titleMap[route.path] || 'ASR 任务管理器'
})
</script>

<style>
body { margin: 0; font-family: 'Helvetica Neue', Helvetica, 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', Arial, sans-serif; }
.app-container { min-height: 100vh; }

.app-aside {
  background: #1d1e1f;
  display: flex;
  flex-direction: column;
  transition: width 0.3s;
  overflow: hidden;
}
.aside-header {
  height: 60px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  border-bottom: 1px solid #333;
  flex-shrink: 0;
}
.app-title {
  font-size: 16px;
  font-weight: 600;
  color: #fff;
  white-space: nowrap;
}
.aside-menu {
  border-right: none !important;
  flex: 1;
}
.aside-menu:not(.el-menu--collapse) {
  width: 200px;
}
.aside-footer {
  border-top: 1px solid #333;
  padding: 8px 0;
  flex-shrink: 0;
}
.collapse-btn {
  color: #bfcbd9;
  width: 100%;
}

.app-header {
  background: #fff;
  display: flex;
  align-items: center;
  justify-content: space-between;
  box-shadow: 0 1px 4px rgba(0,0,0,0.06);
  padding: 0 24px;
  height: 56px;
}
.header-breadcrumb {
  font-size: 16px;
  font-weight: 600;
  color: #303133;
}
.header-right {
  display: flex;
  align-items: center;
  gap: 12px;
}
.version-tag {
  font-size: 12px;
  color: #909399;
  background: #f4f4f5;
  padding: 2px 8px;
  border-radius: 4px;
}

.app-main {
  padding: 20px;
  background: #f5f7fa;
  min-height: calc(100vh - 56px);
  box-sizing: border-box;
}
</style>
