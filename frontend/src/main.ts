import { createApp } from "vue";
import { createPinia } from "pinia";
import ElementPlus from "element-plus";
import "element-plus/dist/index.css";
import App from "./App.vue";
import router from "./router";
import { useAuthStore } from "./stores/auth";

const app = createApp(App);
const pinia = createPinia();
app.use(pinia).use(router).use(ElementPlus);
router.beforeEach(async (to) => {
  const auth = useAuthStore(pinia);
  await auth.init();
  const access = String(to.meta.access ?? "public");
  if (access !== "public" && !auth.loggedIn) {
    return { path: "/login", query: { redirect: to.fullPath } };
  }
  if (access === "content" && !auth.isContentManager) return "/chat";
  if (access === "admin" && !auth.isAdmin) return "/chat";
  return true;
});
// 先静默恢复登录态；失败时仍以访客身份正常使用公开知识库。
useAuthStore(pinia).init();
app.mount("#app");
