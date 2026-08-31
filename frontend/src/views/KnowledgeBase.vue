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

    <!-- chunk 明细抽屉：每块内容 + 大小 + 位置元数据 -->
    <el-drawer v-model="showChunks" :title="chunksTitle" size="520px">
      <div v-if="chunksLoading" class="chunks-loading">加载中...</div>
      <el-scrollbar v-else class="chunks-scroll">
        <div v-for="c in chunks" :key="c.chunk_index" class="chunk-item">
          <div class="chunk-meta">
            <el-tag size="small">#{{ c.chunk_index }}</el-tag>
            <span class="chunk-size">{{ c.char_length }} 字符 · ≈{{ c.token_estimate }} tokens</span>
            <span v-if="c.page != null" class="chunk-loc">P{{ c.page }}</span>
            <span v-else-if="c.slide_number != null" class="chunk-loc">slide {{ c.slide_number }}</span>
            <span v-else-if="c.sheet_name" class="chunk-loc">{{ c.sheet_name }}[{{ c.row_range }}]</span>
          </div>
          <pre class="chunk-text">{{ c.text }}</pre>
        </div>
        <el-empty v-if="!chunks.length" description="该文档没有 chunk" />
      </el-scrollbar>
    </el-drawer>
  </el-container>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import DocumentUpload from "../components/DocumentUpload.vue";
import { useKnowledgeBasesStore } from "../stores/knowledgeBases";
import {
  deleteDocument as apiDeleteDocument,
  getDocChunks,
  getKnowledgeBaseDetail,
  getStats,
  reindex as apiReindex,
  reindexStatus,
} from "../api/knowledgeBases";
import type { ChunkItem, DocumentItem, KnowledgeBase, Stats } from "../types";

const kbStore = useKnowledgeBasesStore();
const stats = ref<Stats>({ document_count: 0, chunk_count: 0, storage_size: 0, knowledge_base_count: 0, vector_dim: 1024, knowledge_bases: [] });

const showCreate = ref(false);
const createName = ref("");
const createDesc = ref("");

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

async function openChunks(row: DocumentItem) {
  showChunks.value = true;
  chunksTitle.value = `${row.filename} 的 chunk（共 ${row.chunk_count} 块）`;
  chunksLoading.value = true;
  chunks.value = [];
  try {
    const data = await getDocChunks(row.doc_id);
    chunks.value = data.chunks;
    chunksTitle.value = `${row.filename} 的 chunk（共 ${data.total} 块）`;
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
    await kbStore.createKb(createName.value.trim(), createDesc.value.trim() || undefined);
    ElMessage.success("知识库已创建");
    showCreate.value = false;
    createName.value = "";
    createDesc.value = "";
    await refreshStats();
  } catch (e) {
    ElMessage.error((e as Error).message);
  }
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
    detailKb.value = { id: detail.id, name: detail.name, description: detail.description, embedding_model: detail.embedding_model, document_count: detail.document_count, chunk_count: detail.chunk_count, created_at: detail.created_at };
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
  await Promise.all([kbStore.load(), refreshStats()]);
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
}
.chunks-loading {
  text-align: center;
  color: var(--el-text-color-secondary);
  padding: 40px 0;
}
.chunks-scroll {
  height: calc(100vh - 80px);
}
.chunk-item {
  border: 1px solid var(--el-border-color-light);
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: 12px;
}
.chunk-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 6px;
}
.chunk-size {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.chunk-loc {
  font-size: 12px;
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
  padding: 1px 6px;
  border-radius: 4px;
}
.chunk-text {
  margin: 0;
  font-size: 13px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: inherit;
  max-height: 220px;
  overflow-y: auto;
}
</style>
