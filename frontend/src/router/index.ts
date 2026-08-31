import { createRouter, createWebHistory } from "vue-router";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: "/chat" },
    { path: "/chat", name: "chat", component: () => import("../views/ChatView.vue") },
    { path: "/knowledge-bases", name: "knowledge-bases", component: () => import("../views/KnowledgeBase.vue") },
  ],
});

export default router;
