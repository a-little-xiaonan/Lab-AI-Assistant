// 知识库状态：唯一数据源（聊天页与管理页共用，管理页修改 → 聊天页自动同步）
import { defineStore } from "pinia";
import {
  createKnowledgeBase as apiCreateKb,
  deleteKnowledgeBase as apiDeleteKb,
  listKnowledgeBases,
} from "../api/knowledgeBases";
import type { KnowledgeBase } from "../types";

export const useKnowledgeBasesStore = defineStore("knowledgeBases", {
  state: () => ({
    knowledgeBases: [] as KnowledgeBase[],
    loading: false,
  }),

  actions: {
    async load() {
      this.loading = true;
      try {
        this.knowledgeBases = await listKnowledgeBases();
      } finally {
        this.loading = false;
      }
    },

    async createKb(
      name: string,
      description?: string,
      accessLevel: "guest" | "student" | "editor" | "admin" = "guest",
    ) {
      await apiCreateKb(name, description, accessLevel);
      await this.load();
    },

    async deleteKb(id: string) {
      await apiDeleteKb(id);
      await this.load(); // 内部处理选中库回落的清理
    },
  },
});
