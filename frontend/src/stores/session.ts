// 会话状态：消息数组是唯一数据源；流式 delta 追加、done 写 sources、error 置顶部提示
// 知识库选择（kbId/列表）在 stores/knowledgeBases.ts（与管理页共用，Phase 3-04 迁出）
import { defineStore } from "pinia";
import { reactive } from "vue";
import { chatStream } from "../api/chat";
import {
  createSession as apiCreateSession,
  deleteSession as apiDeleteSession,
  getSession,
  listSessions,
} from "../api/sessions";
import { useKnowledgeBasesStore } from "./knowledgeBases";
import type { MessageItem, SessionItem, Source } from "../types";

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  sources: Source[];
  streaming: boolean; // 当前正在流式生成（占位消息）
  error?: string;
}

const STORAGE_KEY = "rag_current_session";

export const useSessionStore = defineStore("session", {
  state: () => ({
    sessions: [] as SessionItem[],
    currentSessionId: null as string | null,
    messages: [] as ChatMessage[],
    streaming: false,
    streamError: "" as string,
    loadingSessions: false,
    abortController: null as AbortController | null,
  }),

  getters: {
    currentSession(state): SessionItem | undefined {
      return state.sessions.find((s) => s.id === state.currentSessionId);
    },
  },

  actions: {
    async init() {
      const kbStore = useKnowledgeBasesStore();
      await kbStore.load();
      kbStore.restoreSelection();
      await this.loadSessions();
      const savedId = localStorage.getItem(STORAGE_KEY);
      if (savedId && this.sessions.some((s) => s.id === savedId)) {
        await this.switchSession(savedId);
      }
    },

    async loadSessions() {
      this.loadingSessions = true;
      try {
        this.sessions = await listSessions();
      } finally {
        this.loadingSessions = false;
      }
    },

    async createSession() {
      const kbId = useKnowledgeBasesStore().kbId;
      const session = await apiCreateSession(kbId || undefined);
      this.sessions.unshift(session);
      await this.switchSession(session.id);
      return session;
    },

    async deleteSession(id: string) {
      await apiDeleteSession(id);
      this.sessions = this.sessions.filter((s) => s.id !== id);
      if (this.currentSessionId === id) {
        this.currentSessionId = null;
        this.messages = [];
        localStorage.removeItem(STORAGE_KEY);
      }
    },

    async switchSession(id: string) {
      if (this.streaming) this.stopStreaming();
      this.currentSessionId = id;
      localStorage.setItem(STORAGE_KEY, id);
      const detail = await getSession(id);
      this.messages = detail.messages.map((m: MessageItem) => ({
        role: m.role,
        content: m.content,
        sources: [],
        streaming: false,
      }));
    },

    stopStreaming() {
      if (this.abortController) {
        this.abortController.abort();
        this.abortController = null;
      }
      this.streaming = false;
      const last = this.messages[this.messages.length - 1];
      if (last && last.streaming) last.streaming = false;
    },

    async sendMessage(text: string) {
      const trimmed = text.trim();
      if (!trimmed || this.streaming) return;
      this.streamError = "";
      if (!this.currentSessionId) await this.createSession();

      // user 消息 + assistant 占位
      this.messages.push({ role: "user", content: trimmed, sources: [], streaming: false });
      // 必须用 reactive 创建：直接 push 普通对象会被 Vue 转成另一个代理，
      // 之后对原对象的增量修改不会触发渲染（经典坑：切会话才显示）
      const assistantMsg: ChatMessage = reactive({
        role: "assistant",
        content: "",
        sources: [],
        streaming: true,
      });
      this.messages.push(assistantMsg);

      this.streaming = true;
      this.abortController = new AbortController();
      // 长消息批量渲染：rAF 节流合入，避免每 token 触发 DOM 更新
      let pending = "";
      let rafId = 0;
      const flush = () => {
        if (pending) {
          assistantMsg.content += pending;
          pending = "";
        }
        rafId = 0;
      };
      try {
        for await (const evt of chatStream(
          {
            session_id: this.currentSessionId,
            knowledge_base_id: useKnowledgeBasesStore().kbId,
            message: trimmed,
            stream: true,
          },
          this.abortController.signal,
        )) {
          if (evt.event === "meta") {
            this.currentSessionId = evt.data.session_id;
            localStorage.setItem(STORAGE_KEY, evt.data.session_id);
          } else if (evt.event === "delta") {
            pending += evt.data.text;
            if (!rafId) rafId = requestAnimationFrame(flush);
          } else if (evt.event === "done") {
            flush();
            assistantMsg.content = evt.data.full_text; // 以 done 帧为准（契约）
            assistantMsg.sources = evt.data.sources;
            assistantMsg.streaming = false;
            await this.loadSessions();
          } else if (evt.event === "error") {
            flush();
            assistantMsg.streaming = false;
            assistantMsg.error = evt.data.message;
            this.streamError = evt.data.message;
          }
        }
        if (assistantMsg.streaming) flush(); // 流自然结束时兜底合入
      } catch (err: unknown) {
        flush();
        assistantMsg.streaming = false;
        if ((err as Error)?.name === "AbortError") {
          // 用户主动停止：已收内容保留
        } else {
          const message = (err as Error)?.message || "网络错误";
          assistantMsg.error = message;
          this.streamError = message;
        }
      } finally {
        if (rafId) cancelAnimationFrame(rafId);
        this.streaming = false;
        this.abortController = null;
      }
    },
  },
});
