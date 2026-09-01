import { defineStore } from "pinia";
import * as authApi from "../api/auth";
import type { AuthUser } from "../types";

export const useAuthStore = defineStore("auth", {
  state: () => ({ user: null as AuthUser | null, initialized: false }),
  getters: {
    loggedIn: (state) => !!state.user,
    isAdmin: (state) => !!state.user?.roles.includes("admin"),
  },
  actions: {
    async init() {
      if (this.initialized) return;
      try {
        const data = await authApi.refresh();
        this.user = data.user;
      } catch {
        this.user = null; // 未登录是正常状态，不弹错误。
      } finally {
        this.initialized = true;
      }
    },
    async login(username: string, password: string) {
      const data = await authApi.login(username, password);
      this.user = data.user;
    },
    async register(payload: { username: string; password: string; nickname: string; email?: string }) {
      await authApi.register(payload);
      await this.login(payload.username, payload.password);
    },
    async logout() {
      await authApi.logout();
      this.user = null;
    },
  },
});
