import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  { path: '/', redirect: '/upload' },
  { path: '/upload', name: 'Upload', component: () => import('../views/UploadView.vue') },
  { path: '/tasks', name: 'Tasks', component: () => import('../views/TaskListView.vue') },
  { path: '/tasks/:taskId', name: 'TaskDetail', component: () => import('../views/TaskDetailView.vue') },
  { path: '/monitor', name: 'Monitor', component: () => import('../views/MonitorView.vue') },
]

export default createRouter({ history: createWebHistory(), routes })
