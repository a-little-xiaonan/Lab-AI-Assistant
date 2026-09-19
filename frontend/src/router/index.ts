import { createRouter, createWebHistory } from "vue-router";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: "/chat" },
    { path: "/login", name: "login", component: () => import("../views/identity/LoginView.vue"), meta: { access: "public" } },
    { path: "/chat", name: "chat", component: () => import("../views/conversation/ChatView.vue"), meta: { access: "public" } },
    {
      path: "/knowledge-bases",
      name: "knowledge-bases",
      component: () => import("../views/knowledge/KnowledgeBase.vue"),
      meta: { access: "content" },
    },
    {
      path: "/knowledge-bases/reviews",
      name: "document-reviews",
      component: () => import("../views/knowledge/DocumentReviewView.vue"),
      meta: { access: "content" },
    },
    { path: "/profile", name: "profile", component: () => import("../views/identity/ProfileView.vue"), meta: { access: "authenticated" } },
    {
      path: "/admin",
      component: () => import("../views/admin/AdminLayout.vue"),
      meta: { access: "content" },
      children: [
        { path: "", redirect: "/admin/overview" },
        {
          path: "overview",
          name: "admin-overview",
          component: () => import("../views/admin/AdminOverviewView.vue"),
          meta: { access: "content" },
        },
        {
          path: "users",
          name: "admin-users",
          component: () => import("../views/admin/identity/AdminUsersView.vue"),
          meta: { access: "admin" },
        },
        { path: "role-applications", name: "admin-role-applications", component: () => import("../views/admin/identity/RoleApplicationsView.vue"), meta: { access: "admin" } },
        { path: "audit-logs", name: "admin-audit-logs", component: () => import("../views/admin/operations/AuditLogsView.vue"), meta: { access: "admin" } },
        { path: "evaluations", name: "admin-evaluations", component: () => import("../views/admin/operations/EvaluationView.vue"), meta: { access: "admin" } },
        { path: "jobs", name: "admin-jobs", component: () => import("../views/admin/operations/JobsView.vue"), meta: { access: "content" } },
        { path: "feedback", name: "admin-feedback", component: () => import("../views/admin/operations/FeedbackView.vue"), meta: { access: "content" } },
      ],
    },
    { path: "/:pathMatch(.*)*", redirect: "/chat" },
  ],
});

export default router;
