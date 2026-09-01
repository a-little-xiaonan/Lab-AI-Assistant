<template>
  <el-container class="kb-page">
    <el-header class="kb-header">
      <el-page-header content="知识库管理" @back="$router.push('/chat')" />
      <el-button type="primary" @click="showCreate = true">＋ 新建知识库</el-button>
    </el-header>

    <el-main>
      <!-- 统计栏 -->
      <el-row :gutter="16" class="stats-row">
        <el-col :span="6"><el-card shadow="never"><div class="stat"><div class="stat-num">{{ stats.document_count }}</div><div class="stat-label">文档总数</div></div></el-card></el-col>
        <el-col :span="6"><el-card shadow="never"><div class="stat"><div class="stat-num">{{ stats.chunk_count }}</div><div class="stat-label">chunk 总数</div></div></el-card></el-col>
        <el-col :span="6"><el-card shadow="never"><div class="stat"><div class="stat-num">{{ stats.knowledge_base_count }}</div><div class="stat-label">知识库数</div></div></el-card></el-col>
        <el-col :span="6"><el-card shadow="never"><div class="stat"><div class="stat-num">{{ stats.vector_dim }}</div><div class="stat-label">向量维度</div></div></el-card></el-col>
      </el-row>

      <!-- 知识库卡片网格 -->
      <el-row :gutter="16" class="kb-grid">
        <el-col v-for="kb in kbStore.knowledgeBases" :key="kb.id" :span="8">
          <el-card shadow="hover" class="kb-card">
            <template #header>
              <div class="kb-card-header">
                <span class="kb-name">{{ kb.name }}</span>
                <el-tag v-if="kb.id === 'kb_default'" size="small" type="info">默认</el-tag>
                <el-tag size="small" effect="plain">{{ accessLevelLabel(kb.access_level) }}</el-tag>
              </div>
            </template>
            <p class="kb-desc">{{ kb.description || "（无描述）" }}</p>
            <div class="kb-stats">
              <span>{{ kb.document_count }} 文档</span> · <span>{{ kb.chunk_count }} chunks</span>
            </div>
            <div class="kb-actions">
              <el-button size="small" @click="openDetail(kb.id)">管理文档</el-button>
              <el-button size="small" type="warning" :loading="reindexingId === kb.id" @click="confirmReindex(kb)">重新索引</el-button>
              <el-button size="small" type="danger" :disabled="kb.id === 'kb_default'" @click="confirmDeleteKb(kb)">删除</el-button>
            </div>
          </el-card>
        </el-col>
      </el-row>
    </el-main>

    <!-- 新建知识库弹窗 -->
    <el-dialog v-model="showCreate" title="新建知识库" width="420px">
      <el-form label-width="70px">
        <el-form-item label="名称" required>
          <el-input v-model="createName" placeholder="知识库名称" maxlength="50" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="createDesc" type="textarea" :rows="2" placeholder="可选" />
        </el-form-item>
        <el-form-item label="知识库等级">
          <el-select v-model="createAccessLevel" style="width: 100%">
            <el-option label="游客级：所有人可读取" value="guest" />
            <el-option label="学生级：登录学生及以上可读取" value="student" />
            <el-option label="编辑级：编辑者及管理员可读取" value="editor" />
            <el-option label="管理员级：仅管理员可读取" value="admin" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showCreate = false">取消</el-button>
        <el-button type="primary" :disabled="!createName.trim()" @click="doCreate">创建</el-button>
      </template>
    </el-dialog>

    <!-- 库详情抽屉：文档列表 -->
    <el-drawer v-model="showDetail" :title="detailKb?.name ?? '文档管理'" size="560px">
      <DocumentUpload v-if="detailKbId" :kb-id="detailKbId" @uploaded="refreshDetail" />
      <el-table :data="detailDocs" size="small" style="margin-top: 12px">
        <el-table-column prop="filename" label="文件名" min-width="140" show-overflow-tooltip />
        <el-table-column prop="chunk_count" label="chunks" width="70" />
        <el-table-column label="主题" min-width="110" show-overflow-tooltip>
          <template #default="{ row }">
            <span>{{ topicNames(row.topics).join("、") || "未标注" }}</span>
            <el-tag v-if="pendingCount(row)" size="small" type="warning" style="margin-left: 5px">
              AI 待审 {{ pendingCount(row) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag v-if="row.status === 'ready'" type="success" size="small">ready</el-tag>
            <el-tag v-else-if="row.status === 'failed'" type="danger" size="small">failed</el-tag>
            <el-tag v-else size="small" type="warning">
              {{ row.status === "processing" ? "处理中" : "重建中" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="160">
          <template #default="{ row }">
            <el-button link size="small" @click="openChunks(row)">查看</el-button>
            <el-button v-if="auth.isAdmin" link size="small" @click="openTopics(row)">审核主题</el-button>
            <el-button link size="small" :loading="reindexingId === row.doc_id" @click="reindexDoc(row.doc_id)">重建</el-button>
            <el-button link size="small" type="danger" @click="confirmDeleteDoc(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-alert
        v-if="detailError"
        :title="detailError"
        type="error"
        :closable="false"
        style="margin-top: 8px"
      />
    </el-drawer>

    <el-dialog v-model="showTopics" :title="`${topicDocument?.filename ?? ''} · 资料主题`" width="520px">
      <el-alert type="info" :closable="false" show-icon style="margin-bottom: 12px">
        AI 推荐仅供审核，未审核标签不会参与定向检索；勾选后保存即视为管理员批准。
      </el-alert>
      <el-checkbox-group v-model="selectedTopicCodes">
        <div v-for="topic in availableTopics" :key="topic.code" class="topic-option">
          <el-checkbox :label="topic.code">{{ topic.name }}</el-checkbox>
          <span>{{ topic.aliases.slice(0, 3).join("、") }}</span>
          <el-tag v-if="suggestionFor(topic.code)" size="small" type="warning">
            AI 推荐{{ confidenceText(topic.code) }}
          </el-tag>
        </div>
      </el-checkbox-group>
      <template #footer>
        <el-button @click="showTopics = false">取消</el-button>
        <el-button type="primary" @click="saveTopics">保存主题</el-button>
      </template>
    </el-dialog>

    <!-- chunk 明细：大抽屉 + 左导航右详情 -->
    <el-drawer v-model="showChunks" :title="chunksTitle" size="75%">
      <div v-if="chunksLoading" class="chunks-loading">加载中...</div>
      <div v-else class="chunks-layout">
        <!-- 左：chunk 导航列表 -->
        <div class="chunk-nav">
          <div
            v-for="c in chunks"
            :key="c.chunk_index"
            class="chunk-nav-item"
            :class="{ active: selectedChunk?.chunk_index === c.chunk_index }"
            @click="selectedChunk = c"
          >
            <div class="chunk-nav-title">#{{ c.chunk_index }}</div>
            <div class="chunk-nav-preview">{{ c.text.slice(0, 40) }}</div>
          </div>
          <el-empty v-if="!chunks.length" description="该文档没有 chunk" />
        </div>
        <!-- 右：选中 chunk 详情 -->
        <div v-if="selectedChunk" class="chunk-detail">
          <el-descriptions :column="2" size="small" border class="chunk-meta-table">
            <el-descriptions-item label="chunk 编号">#{{ selectedChunk.chunk_index }}</el-descriptions-item>
            <el-descriptions-item label="大小">
              {{ selectedChunk.char_length }} 字符 · ≈{{ selectedChunk.token_estimate }} tokens
            </el-descriptions-item>
            <el-descriptions-item label="位置">
              <span v-if="selectedChunk.page != null">P{{ selectedChunk.page }}</span>
              <span v-else-if="selectedChunk.slide_number != null">slide {{ selectedChunk.slide_number }}</span>
              <span v-else-if="selectedChunk.sheet_name">{{ selectedChunk.sheet_name }}[{{ selectedChunk.row_range }}]</span>
              <span v-else>—</span>
            </el-descriptions-item>
            <el-descriptions-item label="创建时间">{{ formatTime(selectedChunk.created_at) }}</el-descriptions-item>
            <el-descriptions-item label="修改时间">{{ formatTime(selectedChunk.updated_at) }}</el-descriptions-item>
            <el-descriptions-item label="文档 ID">{{ selectedDocId }}</el-descriptions-item>
          </el-descriptions>
          <div class="chunk-detail-text">
            <pre>{{ selectedChunk.text }}</pre>
          </div>
        </div>
        <el-empty v-else description="选择左侧 chunk 查看详情" />
      </div>
    </el-drawer>
  </el-container>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import DocumentUpload from "../components/DocumentUpload.vue";
import { useKnowledgeBasesStore } from "../stores/knowledgeBases";
import { useAuthStore } from "../stores/auth";
import {
  deleteDocument as apiDeleteDocument,
  getDocChunks,
  getKnowledgeBaseDetail,
  getStats,
  listRetrievalTopics,
  reindex as apiReindex,
  reindexStatus,
  updateDocumentTopics,
} from "../api/knowledgeBases";
import type { ChunkItem, DocumentItem, KnowledgeBase, RetrievalTopic, Stats } from "../types";

const kbStore = useKnowledgeBasesStore();
const auth = useAuthStore();
const stats = ref<Stats>({ document_count: 0, chunk_count: 0, storage_size: 0, knowledge_base_count: 0, vector_dim: 1024, knowledge_bases: [] });

const showCreate = ref(false);
const createName = ref("");
const createDesc = ref("");
const createAccessLevel = ref<"guest" | "student" | "editor" | "admin">("guest");

const showDetail = ref(false);
const detailKbId = ref("");
const detailKb = ref<KnowledgeBase | null>(null);
const detailDocs = ref<DocumentItem[]>([]);
const detailError = ref("");
const reindexingId = ref(""); // 正在重建的 doc_id 或 kb_id（按钮 loading）
const timers: number[] = [];

// chunk 明细
const showChunks = ref(false);
const chunks = ref<ChunkItem[]>([]);
const chunksTitle = ref("");
const chunksLoading = ref(false);
const selectedChunk = ref<ChunkItem | null>(null);
const selectedDocId = ref("");
const showTopics = ref(false);
const topicDocument = ref<DocumentItem | null>(null);
const availableTopics = ref<RetrievalTopic[]>([]);
const selectedTopicCodes = ref<string[]>([]);

function formatTime(t: string | undefined | null): string {
  if (!t) return "—";
  return new Date(t).toLocaleString("zh-CN", { hour12: false });
}

function topicNames(codes: string[] = []): string[] {
  const names = new Map(availableTopics.value.map((topic) => [topic.code, topic.name]));
  return codes.map((code) => names.get(code) || code);
}

function pendingCount(row: DocumentItem): number {
  return row.topic_suggestions?.filter((item) => item.review_status === "pending").length || 0;
}

function suggestionFor(code: string) {
  return topicDocument.value?.topic_suggestions?.find(
    (item) => item.topic_code === code && item.review_status === "pending",
  );
}

function confidenceText(code: string): string {
  const confidence = suggestionFor(code)?.confidence;
  return confidence == null ? "" : ` ${Math.round(confidence * 100)}%`;
}

async function openTopics(row: DocumentItem) {
  topicDocument.value = row;
  selectedTopicCodes.value = [
    ...(row.topics || []),
    ...(row.topic_suggestions || [])
      .filter((item) => item.review_status === "pending")
      .map((item) => item.topic_code),
  ];
  showTopics.value = true;
  try {
    if (!availableTopics.value.length) availableTopics.value = await listRetrievalTopics();
  } catch (error) {
    ElMessage.error((error as Error).message);
  }
}

async function saveTopics() {
  if (!topicDocument.value) return;
  try {
    const topics = await updateDocumentTopics(detailKbId.value, topicDocument.value.doc_id, selectedTopicCodes.value);
    topicDocument.value.topics = topics;
    await refreshDetail();
    ElMessage.success("文档主题已保存");
    showTopics.value = false;
  } catch (error) {
    ElMessage.error((error as Error).message);
  }
}

async function openChunks(row: DocumentItem) {
  showChunks.value = true;
  chunksTitle.value = `${row.filename} 的 chunk`;
  chunksLoading.value = true;
  chunks.value = [];
  selectedChunk.value = null;
  selectedDocId.value = row.doc_id;
  try {
    const data = await getDocChunks(row.doc_id);
    chunks.value = data.chunks;
    chunksTitle.value = `${row.filename} 的 chunk（共 ${data.total} 块）`;
    if (data.chunks.length) selectedChunk.value = data.chunks[0];
  } catch (e) {
    ElMessage.error((e as Error).message);
  } finally {
    chunksLoading.value = false;
  }
}

async function refreshStats() {
  stats.value = await getStats();
}

async function doCreate() {
  try {
    await kbStore.createKb(createName.value.trim(), createDesc.value.trim() || undefined, createAccessLevel.value);
    ElMessage.success("知识库已创建");
    showCreate.value = false;
    createName.value = "";
    createDesc.value = "";
    createAccessLevel.value = "guest";
    await refreshStats();
  } catch (e) {
    ElMessage.error((e as Error).message);
  }
}

function accessLevelLabel(level: KnowledgeBase["access_level"]): string {
  return { guest: "游客级", student: "学生级", editor: "编辑级", admin: "管理员级" }[level];
}

async function confirmDeleteKb(kb: KnowledgeBase) {
  // 防呆：输入知识库名称确认
  try {
    const { value } = await ElMessageBox.prompt(
      `删除知识库「${kb.name}」将级联删除全部文档与向量，不可恢复。请输入知识库名称确认：`,
      "删除知识库",
      { confirmButtonText: "确认删除", cancelButtonText: "取消", inputPlaceholder: kb.name },
    );
    if (value.trim() !== kb.name) {
      ElMessage.error("名称不匹配，未删除");
      return;
    }
    await kbStore.deleteKb(kb.id);
    ElMessage.success("知识库已删除");
    await refreshStats();
  } catch {
    /* 取消 */
  }
}

async function openDetail(kbId: string) {
  detailKbId.value = kbId;
  showDetail.value = true;
  await refreshDetail();
}

async function refreshDetail() {
  if (!detailKbId.value) return;
  try {
    const detail = await getKnowledgeBaseDetail(detailKbId.value);
    detailKb.value = { id: detail.id, name: detail.name, description: detail.description, embedding_model: detail.embedding_model, access_level: detail.access_level, document_count: detail.document_count, chunk_count: detail.chunk_count, created_at: detail.created_at };
    detailDocs.value = detail.documents;
    detailError.value = "";
  } catch (e) {
    detailError.value = (e as Error).message;
  }
}

async function confirmDeleteDoc(row: DocumentItem) {
  try {
    await ElMessageBox.confirm(`删除文档「${row.filename}」？向量与记录一并清除。`, "删除文档", {
      type: "warning",
      confirmButtonText: "删除",
      cancelButtonText: "取消",
    });
    await apiDeleteDocument(detailKbId.value, row.doc_id);
    ElMessage.success("文档已删除");
    await refreshDetail();
    await refreshStats();
  } catch {
    /* 取消 */
  }
}

async function confirmReindex(kb: KnowledgeBase) {
  try {
    await ElMessageBox.confirm(`重新索引知识库「${kb.name}」全部文档？重建期间检索不受影响。`, "重新索引", {
      type: "warning",
      confirmButtonText: "开始重建",
      cancelButtonText: "取消",
    });
    await apiReindex(kb.id);
    reindexingId.value = kb.id;
    pollReindex(kb.id);
  } catch {
    /* 取消 */
  }
}

async function reindexDoc(docId: string) {
  try {
    await apiReindex(detailKbId.value, docId);
    reindexingId.value = docId;
    pollReindex(detailKbId.value);
  } catch (e) {
    ElMessage.error((e as Error).message);
  }
}

/** 轮询重建状态直到非 running，然后刷新列表 */
function pollReindex(kbId: string) {
  const timer = window.setInterval(async () => {
    const status = await reindexStatus(kbId);
    if (status.status === "running") return;
    window.clearInterval(timer);
    reindexingId.value = "";
    if (status.status === "failed") {
      ElMessage.error(`重建失败：${status.error_message || "未知原因"}`);
    } else {
      ElMessage.success(`重建完成：chunks ${status.docs_before} → ${status.docs_after}`);
    }
    await refreshDetail();
    await refreshStats();
    kbStore.load();
  }, 2000);
  timers.push(timer);
}

onMounted(async () => {
  await Promise.all([kbStore.load(), refreshStats(), listRetrievalTopics().then((items) => { availableTopics.value = items; })]);
});

onBeforeUnmount(() => timers.forEach((t) => window.clearInterval(t)));
</script>

<style scoped>
.kb-page {
  height: 100%;
}
.kb-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid var(--el-border-color-light);
}
.stats-row {
  margin-bottom: 16px;
}
.stat {
  text-align: center;
}
.stat-num {
  font-size: 22px;
  font-weight: 600;
}
.stat-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-top: 2px;
}
.kb-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.kb-name {
  font-weight: 600;
}
.kb-desc {
  font-size: 13px;
  color: var(--el-text-color-secondary);
  min-height: 20px;
  margin: 0 0 8px;
}
.kb-stats {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-bottom: 10px;
}
.kb-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.permission-add {
  display: flex;
  gap: 10px;
  margin-bottom: 14px;
}
.topic-option {
  display: flex;
  align-items: center;
  gap: 12px;
  min-height: 34px;
}
.topic-option span {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.chunks-loading {
  text-align: center;
  color: var(--el-text-color-secondary);
  padding: 40px 0;
}
.chunks-layout {
  display: flex;
  gap: 16px;
  height: calc(100vh - 90px);
}
.chunk-nav {
  width: 220px;
  flex-shrink: 0;
  overflow-y: auto;
  border-right: 1px solid var(--el-border-color-light);
  padding-right: 8px;
}
.chunk-nav-item {
  padding: 8px 10px;
  border-radius: 6px;
  cursor: pointer;
  margin-bottom: 6px;
  border: 1px solid var(--el-border-color-lighter);
}
.chunk-nav-item:hover {
  background: var(--el-fill-color-light);
}
.chunk-nav-item.active {
  background: var(--el-color-primary-light-9);
  border-color: var(--el-color-primary);
}
.chunk-nav-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--el-color-primary);
}
.chunk-nav-preview {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-top: 2px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.chunk-detail {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.chunk-meta-table {
  margin-bottom: 12px;
}
.chunk-detail-text {
  flex: 1;
  min-height: 0;
  border: 1px solid var(--el-border-color-light);
  border-radius: 8px;
  overflow: auto;
  background: var(--el-fill-color-lighter);
}
.chunk-detail-text pre {
  margin: 0;
  padding: 14px 16px;
  font-size: 14px;
  line-height: 1.8;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: inherit;
}
</style>
