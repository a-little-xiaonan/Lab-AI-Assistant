// 知识库状态：唯一数据源（聊天页与管理页共用，管理页修改 → 聊天页自动同步）
import { defineStore } from "pinia";
import {
  createKnowledgeBase as apiCreateKb,
  deleteKnowledgeBase as apiDeleteKb,
  listKnowledgeBases,
} from "../api/knowledgeBases";
import type { KnowledgeBase } from "../types";

const KB_STORAGE_KEY = "rag_current_kb";

export const useKnowledgeBasesStore = defineStore("knowledgeBases", {
  state: () => ({
    knowledgeBases: [] as KnowledgeBase[],
    kbId: "" as string,
    loading: false,
  }),

  getters: {
    currentKb(state): KnowledgeBase | undefined {
      return state.knowledgeBases.find((k) => k.id === state.kbId);
    },
  },

  actions: {
    async load() {
      this.loading = true;
      try {
        this.knowledgeBases = await listKnowledgeBases();
        if (!this.kbId && this.knowledgeBases.length) {
          this.kbId = this.knowledgeBases[0].id;
        }
        if (this.kbId && !this.knowledgeBases.some((k) => k.id === this.kbId)) {
          this.kbId = this.knowledgeBases[0]?.id ?? ""; // 选中库被删 → 回落
        }
      } finally {
        this.loading = false;
      }
    },

    selectKb(id: string) {
      this.kbId = id;
      localStorage.setItem(KB_STORAGE_KEY, id);
    },

    restoreSelection() {
      const saved = localStorage.getItem(KB_STORAGE_KEY);
      if (saved && this.knowledgeBases.some((k) => k.id === saved)) {
        this.kbId = saved;
      }
    },

    async createKb(name: string, description?: string) {
      await apiCreateKb(name, description);
      await this.load();
    },

    async deleteKb(id: string) {
      await apiDeleteKb(id);
      await this.load(); // 内部处理选中库回落的清理
      if (this.kbId === id) {
        this.kbId = this.knowledgeBases[0]?.id ?? "";
      }
      if (!this.kbId) localStorage.removeItem(KB_STORAGE_KEY);
    },
  },
});
