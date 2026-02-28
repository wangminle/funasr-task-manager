<template>
  <el-container class="app-container">
    <el-header class="app-header">
      <div class="header-left">
        <el-icon :size="24"><Headset /></el-icon>
        <span class="app-title">ASR 任务管理器</span>
      </div>
      <el-menu :default-active="$route.path" mode="horizontal" :ellipsis="false" router class="header-menu">
        <el-menu-item index="/upload">
          <el-icon><Upload /></el-icon>
          <span>文件上传</span>
        </el-menu-item>
        <el-menu-item index="/tasks">
          <el-icon><List /></el-icon>
          <span>任务列表</span>
        </el-menu-item>
        <el-menu-item index="/monitor">
          <el-icon><Monitor /></el-icon>
          <span>系统监控</span>
        </el-menu-item>
      </el-menu>
    </el-header>
    <el-main class="app-main">
      <router-view />
    </el-main>
  </el-container>
</template>

<script setup>
import { Headset, Upload, List, Monitor } from '@element-plus/icons-vue'
</script>

<style>
body { margin: 0; font-family: 'Helvetica Neue', Helvetica, 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', Arial, sans-serif; }
.app-container { min-height: 100vh; background: #f5f7fa; }
.app-header { background: #fff; display: flex; align-items: center; box-shadow: 0 1px 4px rgba(0,0,0,0.08); padding: 0 20px; }
.header-left { display: flex; align-items: center; gap: 8px; margin-right: 40px; }
.app-title { font-size: 18px; font-weight: 600; color: #303133; }
.header-menu { border-bottom: none !important; }
.app-main { padding: 20px; max-width: 1200px; margin: 0 auto; width: 100%; box-sizing: border-box; }
</style>
