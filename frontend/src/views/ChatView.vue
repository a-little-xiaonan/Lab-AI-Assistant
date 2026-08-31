<template>
  <el-container class="chat-layout">
    <!-- 侧栏：会话列表 -->
    <el-aside width="240px" class="sidebar">
      <div class="sidebar-header">
        <el-button type="primary" size="small" style="width: 100%" @click="store.createSession()">
          ＋ 新建会话
        </el-button>
        <el-button size="small" style="width: 100%; margin: 8px 0 0" @click="$router.push('/knowledge-bases')">
          📚 知识库管理
        </el-button>
      </div>
      <el-scrollbar class="session-list">
        <div
          v-for="s in store.sessions"
          :key="s.id"
          class="session-item"
          :class="{ active: s.id === store.currentSessionId }"
          @click="store.switchSession(s.id)"
        >
          <span class="session-title">{{ s.id.slice(0, 8) }}</span>
          <el-button
            v-if="s.id === store.currentSessionId"
            link
            type="danger"
            size="small"
            class="session-delete"
            @click.stop="confirmDelete(s)"
          >
            删
          </el-button>
        </div>
      </el-scrollbar>
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
        <el-button link @click="$router.push('/settings')">设置</el-button>
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
import { onMounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import ChatMessage from "../components/ChatMessage.vue";
import MessageInput from "../components/MessageInput.vue";
import { useSessionStore } from "../stores/session";
import { useKnowledgeBasesStore } from "../stores/knowledgeBases";

const store = useSessionStore();
const kbStore = useKnowledgeBasesStore();
const scrollbarRef = ref();

function scrollToBottom() {
  requestAnimationFrame(() => {
    const el = scrollbarRef.value;
    if (el) el.setScrollTop(el.wrapRef?.scrollHeight ?? 0);
  });
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
.session-title {
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.session-delete {
  flex-shrink: 0;
}
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid var(--el-border-color-light);
}
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
