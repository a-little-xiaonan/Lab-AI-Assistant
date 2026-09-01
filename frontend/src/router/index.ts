import { createRouter, createWebHistory } from "vue-router";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: "/chat" },
    { path: "/login", name: "login", component: () => import("../views/LoginView.vue") },
    { path: "/chat", name: "chat", component: () => import("../views/ChatView.vue") },
    { path: "/knowledge-bases", name: "knowledge-bases", component: () => import("../views/KnowledgeBase.vue") },
    { path: "/profile", name: "profile", component: () => import("../views/ProfileView.vue") },
    { path: "/admin/users", name: "admin-users", component: () => import("../views/AdminUsersView.vue") },
  ],
});

export default router;
