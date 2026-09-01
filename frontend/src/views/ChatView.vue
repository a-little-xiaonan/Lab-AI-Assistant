<template>
  <el-container class="chat-layout">
    <!-- 侧栏：会话列表 -->
    <el-aside width="240px" class="sidebar">
      <div class="sidebar-header">
        <el-button type="primary" size="small" style="width: 100%" @click="store.createSession()">
          ＋ 新建会话
        </el-button>
        <el-button v-if="auth.user?.roles.some((r) => r === 'editor' || r === 'admin')" size="small" style="width: 100%; margin: 8px 0 0" @click="$router.push('/knowledge-bases')">
          📚 知识库管理
        </el-button>
        <el-button v-if="auth.isAdmin" size="small" style="width: 100%; margin: 8px 0 0" @click="$router.push('/admin/users')">
          👥 用户与角色
        </el-button>
        <el-button
          size="small"
          style="width: 100%; margin: 8px 0 0"
          :type="batchMode ? 'warning' : 'default'"
          @click="toggleBatchMode"
        >
          {{ batchMode ? "退出批量" : "🗑 批量删除" }}
        </el-button>
      </div>
      <el-scrollbar class="session-list">
        <div v-if="store.sessions.length > 5" class="session-count">
          共 {{ store.sessions.length }} 个会话
          <el-button link size="small" type="primary" @click="showAllSessions = !showAllSessions">
            {{ showAllSessions ? "收起" : "查看全部" }}
          </el-button>
        </div>
        <div
          v-for="s in displayedSessions"
          :key="s.id"
          class="session-item"
          :class="{ active: !batchMode && s.id === store.currentSessionId, selected: batchMode && selected[s.id] }"
          @click="batchMode ? toggleSelect(s.id) : store.switchSession(s.id)"
        >
          <el-checkbox
            v-if="batchMode"
            :model-value="!!selected[s.id]"
            class="session-check"
            @click.stop="toggleSelect(s.id)"
          />
          <span class="session-title">{{ s.name || "新会话" }}</span>
          <span v-if="!batchMode" class="session-actions" @click.stop>
            <el-button link size="small" title="重命名" @click="renameSession(s)">✎</el-button>
            <el-button
              v-if="s.id === store.currentSessionId"
              link
              type="danger"
              size="small"
              title="删除会话"
              @click="confirmDelete(s)"
            >
              删
            </el-button>
          </span>
        </div>
      </el-scrollbar>
      <!-- 批量操作工具条 -->
      <div v-if="batchMode" class="batch-bar">
        <el-checkbox
          :model-value="displayedSessions.length > 0 && selectedCount === displayedSessions.length"
          @change="selectAll"
        >
          全选
        </el-checkbox>
        <span class="batch-count">已选 {{ selectedCount }}</span>
        <el-button size="small" type="danger" :disabled="!selectedCount" @click="deleteSelected">
          删除所选
        </el-button>
        <el-button size="small" type="danger" plain @click="deleteAll">全部</el-button>
      </div>
    </el-aside>

    <el-container>
      <!-- 顶部：知识库选择器 -->
      <el-header class="header">
        <el-select v-model="kbStore.kbId" placeholder="选择知识库" style="width: 220px" @change="kbStore.selectKb">
          <el-option
            v-for="kb in kbStore.knowledgeBases"
            :key="kb.id"
            :label="`${kb.name}（${kb.document_count} 文档）`"
            :value="kb.id"
          />
        </el-select>
        <div class="user-actions">
          <template v-if="auth.user">
            <el-button link @click="$router.push('/profile')">{{ auth.user.nickname }}</el-button>
            <el-button link type="danger" @click="signOut">退出</el-button>
          </template>
          <el-button v-else link type="primary" @click="$router.push('/login')">登录 / 注册</el-button>
        </div>
      </el-header>

      <el-main class="main">
        <el-alert
          v-if="store.streamError"
          :title="store.streamError"
          type="error"
          :closable="true"
          class="stream-error"
          @close="store.streamError = ''"
        />
        <el-scrollbar ref="scrollbarRef" class="message-list">
          <div v-if="!store.messages.length" class="empty-tip">
            <p>👋 你好，我是 RAG 智能助手</p>
            <p>选择左侧会话，或在下方输入问题开始提问</p>
          </div>
          <ChatMessage
            v-for="(m, i) in store.messages"
            :key="i"
            :message="m"
          />
        </el-scrollbar>
        <MessageInput
          :disabled="!kbStore.kbId"
          :streaming="store.streaming"
          @send="store.sendMessage"
          @stop="store.stopStreaming"
        />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import ChatMessage from "../components/ChatMessage.vue";
import MessageInput from "../components/MessageInput.vue";
import { renameSession as apiRenameSession } from "../api/sessions";
import { useSessionStore } from "../stores/session";
import { useKnowledgeBasesStore } from "../stores/knowledgeBases";
import { useAuthStore } from "../stores/auth";
import type { SessionItem } from "../types";

const store = useSessionStore();
const kbStore = useKnowledgeBasesStore();
const auth = useAuthStore();
const scrollbarRef = ref();
const showAllSessions = ref(false);

/** 会话列表：默认前 5 个，「查看全部」展开（旧会话也能选到） */
const displayedSessions = computed(() =>
  showAllSessions.value ? store.sessions : store.sessions.slice(0, 5),
);

// 批量删除模式
const batchMode = ref(false);
const selected = ref<Record<string, boolean>>({});
const selectedCount = computed(
  () => Object.values(selected.value).filter(Boolean).length,
);

function toggleBatchMode() {
  batchMode.value = !batchMode.value;
  selected.value = {};
}

function toggleSelect(id: string) {
  selected.value[id] = !selected.value[id];
}

function selectAll(checked: boolean | string | number) {
  const on = !!checked;
  const map: Record<string, boolean> = {};
  for (const s of displayedSessions.value) map[s.id] = on;
  selected.value = map;
}

async function deleteSelected() {
  const ids = Object.keys(selected.value).filter((k) => selected.value[k]);
  if (!ids.length) return;
  try {
    await ElMessageBox.confirm(`确定删除选中的 ${ids.length} 个会话？消息与记忆一并清除。`, "批量删除", {
      type: "warning",
      confirmButtonText: "删除",
      cancelButtonText: "取消",
    });
    const deleted = await store.batchDelete(ids);
    ElMessage.success(`已删除 ${deleted} 个会话`);
    batchMode.value = false;
  } catch {
    /* 取消 */
  }
}

async function deleteAll() {
  try {
    await ElMessageBox.confirm(
      `确定删除全部 ${store.sessions.length} 个会话？此操作不可恢复。`,
      "删除全部会话",
      { type: "warning", confirmButtonText: "全部删除", cancelButtonText: "取消" },
    );
    const deleted = await store.batchDelete();
    ElMessage.success(`已删除 ${deleted} 个会话`);
    batchMode.value = false;
  } catch {
    /* 取消 */
  }
}

function scrollToBottom() {
  requestAnimationFrame(() => {
    const el = scrollbarRef.value;
    if (el) el.setScrollTop(el.wrapRef?.scrollHeight ?? 0);
  });
}

async function renameSession(session: SessionItem) {
  try {
    const { value } = await ElMessageBox.prompt("修改会话名称", "重命名", {
      inputValue: session.name || "",
      confirmButtonText: "保存",
      cancelButtonText: "取消",
      inputPlaceholder: "输入会话名称",
    });
    const name = value.trim();
    if (!name) return;
    await apiRenameSession(session.id, name);
    await store.loadSessions();
    ElMessage.success("已重命名");
  } catch {
    /* 取消 */
  }
}

async function confirmDelete(session: { id: string }) {
  try {
    await ElMessageBox.confirm("删除后会话记录不可恢复，确定删除？", "删除会话", {
      type: "warning",
      confirmButtonText: "删除",
      cancelButtonText: "取消",
    });
    await store.deleteSession(session.id);
    ElMessage.success("会话已删除");
  } catch {
    /* 用户取消 */
  }
}

async function signOut() {
  try {
    await auth.logout();
    await store.loadSessions();
    await kbStore.load();
    ElMessage.success("已退出登录");
  } catch (err) {
    ElMessage.error((err as Error).message);
  }
}

onMounted(async () => {
  try {
    await store.init();
  } catch (e) {
    ElMessage.error((e as Error).message);
  }
});

// 消息列表滚动到底（消息数组变化 + 流式增量时）
import { watch } from "vue";
watch(
  () => store.messages.map((m) => m.content).join("").length,
  () => scrollToBottom(),
);
</script>

<style scoped>
.chat-layout {
  height: 100%;
}
.sidebar {
  border-right: 1px solid var(--el-border-color-light);
  display: flex;
  flex-direction: column;
}
.sidebar-header {
  padding: 12px;
}
.session-list {
  flex: 1;
}
.session-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 16px;
  cursor: pointer;
  border-left: 3px solid transparent;
}
.session-item:hover {
  background: var(--el-fill-color-light);
}
.session-item.active {
  background: var(--el-fill-color-light);
  border-left-color: var(--el-color-primary);
}
.session-item.selected {
  background: var(--el-color-primary-light-9);
  border-left-color: var(--el-color-primary);
}
.session-check {
  margin-right: 6px;
}
.session-title {
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
}
.session-actions {
  flex-shrink: 0;
  opacity: 0;
  transition: opacity 0.15s;
}
.session-item:hover .session-actions {
  opacity: 1;
}
.session-count {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  padding: 4px 16px;
}
.batch-bar {
  border-top: 1px solid var(--el-border-color-light);
  padding: 10px 12px;
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}
.batch-count {
  flex: 1;
  color: var(--el-text-color-secondary);
}
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid var(--el-border-color-light);
}
.user-actions { display: flex; align-items: center; gap: 8px; }
.main {
  display: flex;
  flex-direction: column;
  padding: 0;
}
.stream-error {
  margin: 12px;
}
.message-list {
  flex: 1;
  padding: 16px 24px;
}
.empty-tip {
  text-align: center;
  color: var(--el-text-color-secondary);
  margin-top: 80px;
  font-size: 14px;
}
</style>
