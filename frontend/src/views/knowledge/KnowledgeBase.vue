<template>
  <el-container class="kb-page" direction="vertical">
    <ModuleHeader section="knowledge" />
    <el-header class="kb-header" :class="{ 'is-managing': showDetail }">
      <div v-if="!showDetail">
        <span class="header-eyebrow">知识中心</span>
        <h1>知识库工作台</h1>
        <p>管理实验室资料、文档主题与检索索引</p>
      </div>
      <div v-else class="workspace-heading">
        <el-button class="back-button" @click="closeDetail">← 返回知识库</el-button>
        <div>
          <span class="header-eyebrow">文档管理工作台</span>
          <div class="workspace-title-row">
            <h1>{{ detailKb?.name || "正在加载" }}</h1>
            <el-tag v-if="detailKb" effect="plain">{{ accessLevelLabel(detailKb.access_level) }}</el-tag>
          </div>
          <p>{{ detailKb?.description || "管理该知识库的文档、分块、主题与检索索引" }}</p>
        </div>
      </div>
      <div class="header-actions">
        <el-button @click="$router.push('/knowledge-bases/reviews')">审核工作台</el-button>
        <template v-if="!showDetail">
          <el-button type="primary" @click="showCreate = true">＋ 新建知识库</el-button>
        </template>
        <template v-else-if="detailKb">
          <el-button :loading="detailLoading" @click="refreshDetail">刷新</el-button>
          <el-button
            type="warning"
            :loading="reindexingId === detailKb.id"
            @click="confirmReindex(detailKb)"
          >
            重建全库索引
          </el-button>
        </template>
      </div>
    </el-header>

    <el-main v-if="!showDetail" class="overview-main">
      <!-- 统计栏 -->
      <el-row :gutter="16" class="stats-row">
        <el-col :span="6"><el-card shadow="never"><div class="stat"><div class="stat-num">{{ stats.document_count }}</div><div class="stat-label">文档总数</div></div></el-card></el-col>
        <el-col :span="6"><el-card shadow="never"><div class="stat"><div class="stat-num">{{ stats.chunk_count }}</div><div class="stat-label">chunk 总数</div></div></el-card></el-col>
        <el-col :span="6"><el-card shadow="never"><div class="stat"><div class="stat-num">{{ stats.knowledge_base_count }}</div><div class="stat-label">知识库数</div></div></el-card></el-col>
        <el-col :span="6"><el-card shadow="never"><div class="stat"><div class="stat-num">{{ stats.vector_dim }}</div><div class="stat-label">向量维度</div></div></el-card></el-col>
      </el-row>

      <!-- 知识库卡片网格 -->
      <div class="kb-grid">
        <div v-for="kb in kbStore.knowledgeBases" :key="kb.id">
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
        </div>
      </div>
    </el-main>

    <el-main v-else class="workspace-main">
      <el-skeleton v-if="detailLoading && !detailKb" :rows="8" animated />
      <template v-else>
        <section class="workspace-summary">
          <div class="summary-tile summary-tile-primary">
            <span>文档总数</span>
            <strong>{{ detailDocs.length }}</strong>
            <small>当前知识库中的全部资料</small>
          </div>
          <div class="summary-tile">
            <span>可检索文档</span>
            <strong>{{ readyDocumentCount }}</strong>
            <small>已完成解析与分块</small>
          </div>
          <div class="summary-tile">
            <span>Chunk 数量</span>
            <strong>{{ detailKb?.chunk_count || 0 }}</strong>
            <small>参与检索的知识单元</small>
          </div>
          <div class="summary-tile">
            <span>已发布</span>
            <strong>{{ publishedDocumentCount }}</strong>
            <small>通过审核并正式生效</small>
          </div>
        </section>

        <el-card shadow="never" class="upload-card">
          <template #header>
            <div class="panel-heading">
              <div>
                <h2>上传新资料</h2>
                <p>文件上传后会自动解析、分块并进入审核流程</p>
              </div>
              <el-tag type="info" effect="plain">PDF / MD / TXT / DOCX</el-tag>
            </div>
          </template>
          <DocumentUpload v-if="detailKbId" :kb-id="detailKbId" @uploaded="handleDocumentUploaded" />
        </el-card>

        <el-card shadow="never" class="documents-card">
          <template #header>
            <div class="panel-heading documents-heading">
              <div>
                <h2>文档与检索单元</h2>
                <p>共 {{ detailDocs.length }} 份文档，当前显示 {{ filteredDetailDocs.length }} 份</p>
              </div>
              <div class="document-filters">
                <el-input
                  v-model="documentSearch"
                  clearable
                  placeholder="搜索文件名或资料来源"
                  class="document-search"
                />
                <el-select v-model="documentStatusFilter" class="status-filter">
                  <el-option label="全部处理状态" value="all" />
                  <el-option label="已就绪" value="ready" />
                  <el-option label="处理中" value="processing" />
                  <el-option label="处理失败" value="failed" />
                </el-select>
                <el-select v-model="governanceStatusFilter" class="status-filter">
                  <el-option label="全部发布状态" value="all" />
                  <el-option label="已发布" value="published" />
                  <el-option label="待审核" value="pending" />
                </el-select>
              </div>
            </div>
          </template>

          <el-alert
            v-if="detailError"
            :title="detailError"
            type="error"
            :closable="false"
            class="detail-error"
          />

          <el-table
            :data="filteredDetailDocs"
            v-loading="detailLoading"
            stripe
            class="documents-table"
            empty-text="没有符合条件的文档"
          >
            <el-table-column label="文档" min-width="260" fixed>
              <template #default="{ row }">
                <div class="document-cell">
                  <div class="document-icon">{{ documentExtension(row.filename) }}</div>
                  <div class="document-copy">
                    <strong>{{ row.filename }}</strong>
                    <span>{{ row.source_name || "未标注资料来源" }} · {{ formatTime(row.created_at) }}</span>
                  </div>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="Chunk" width="105" align="center">
              <template #default="{ row }"><strong class="chunk-count">{{ row.chunk_count }}</strong></template>
            </el-table-column>
            <el-table-column label="主题" min-width="190">
              <template #default="{ row }">
                <div class="topic-tags">
                  <el-tag v-for="name in topicNames(row.topics).slice(0, 2)" :key="name" size="small" effect="plain">{{ name }}</el-tag>
                  <span v-if="!row.topics?.length" class="muted-text">未标注</span>
                  <el-tag v-if="pendingCount(row)" size="small" type="warning">AI 待审 {{ pendingCount(row) }}</el-tag>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="处理状态" width="120">
              <template #default="{ row }">
                <el-tag :type="processingStatusType(row.status)" effect="light">
                  {{ processingStatusLabel(row.status) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="发布状态" width="135">
              <template #default="{ row }">
                <el-tag :type="row.governance_status === 'published' ? 'success' : 'warning'" effect="plain">
                  {{ row.version_review_status || row.governance_status }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="内容负责人" min-width="130">
              <template #default="{ row }">{{ row.content_owner || "—" }}</template>
            </el-table-column>
            <el-table-column label="操作" width="300" fixed="right">
              <template #default="{ row }">
                <div class="document-actions">
                  <el-button type="primary" plain size="small" @click="openChunks(row)">Chunk</el-button>
                  <el-button size="small" @click="openTopics(row)">主题</el-button>
                  <el-button size="small" @click="openGovernance(row)">治理</el-button>
                  <el-dropdown trigger="click" @command="handleDocumentCommand($event, row)">
                    <el-button size="small">更多 ···</el-button>
                    <template #dropdown>
                      <el-dropdown-menu>
                        <el-dropdown-item command="reindex">重建该文档索引</el-dropdown-item>
                        <el-dropdown-item command="delete" divided class="danger-menu-item">删除文档</el-dropdown-item>
                      </el-dropdown-menu>
                    </template>
                  </el-dropdown>
                </div>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </template>
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
            <el-option label="实验室成员级：成员及管理员可读取" value="editor" :disabled="!auth.isAdmin" />
            <el-option label="管理员级：仅管理员可读取" value="admin" :disabled="!auth.isAdmin" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showCreate = false">取消</el-button>
        <el-button type="primary" :disabled="!createName.trim()" @click="doCreate">创建</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="showTopics" :title="`${topicDocument?.filename ?? ''} · 资料主题`" width="680px">
      <el-alert type="info" :closable="false" show-icon style="margin-bottom: 12px">
        AI 推荐仅供审核，未审核标签不会参与定向检索；勾选后保存即视为实验室成员或管理员批准。
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

    <el-dialog v-model="showGovernance" :title="`${governanceDocument?.filename ?? ''} · 治理信息`" width="680px">
      <el-form label-width="100px">
        <el-form-item label="内容负责人" required><el-input v-model="governanceForm.content_owner" maxlength="128" /></el-form-item>
        <el-form-item label="资料来源" required><el-input v-model="governanceForm.source_name" maxlength="255" /></el-form-item>
        <el-form-item label="敏感等级"><el-select v-model="governanceForm.sensitivity_level" style="width:100%"><el-option label="游客" value="guest"/><el-option label="学生" value="student"/><el-option label="实验室成员" value="editor"/><el-option label="管理员" value="admin"/></el-select></el-form-item>
        <el-form-item label="生效时间"><el-date-picker v-model="governanceForm.effective_at" type="datetime" value-format="YYYY-MM-DDTHH:mm:ss" clearable style="width:100%" /></el-form-item>
        <el-form-item label="过期时间"><el-date-picker v-model="governanceForm.expires_at" type="datetime" value-format="YYYY-MM-DDTHH:mm:ss" clearable style="width:100%" /></el-form-item>
        <el-form-item label="最后复核"><span>{{ formatTime(governanceDocument?.last_reviewed_at) }}</span></el-form-item>
      </el-form>
      <template #footer><el-button @click="showGovernance=false">取消</el-button><el-button type="primary" @click="saveGovernance">保存并记录复核</el-button></template>
    </el-dialog>

    <!-- chunk 明细：大抽屉 + 左导航右详情 -->
    <el-drawer v-model="showChunks" :title="chunksTitle" size="92%" class="chunk-drawer">
      <div v-if="chunksLoading" class="chunks-loading">加载中...</div>
      <div v-else class="chunks-layout">
        <!-- 左：chunk 导航列表 -->
        <div class="chunk-nav-wrapper">
          <div class="chunk-nav">
            <div
              v-for="c in chunks"
              :key="c.chunk_index"
              class="chunk-nav-item"
              :class="{ active: selectedChunk?.chunk_index === c.chunk_index }"
              @click="selectChunk(c)"
            >
              <div class="chunk-nav-title">#{{ c.chunk_index }}</div>
              <div class="chunk-nav-preview">{{ c.text.slice(0, 40) }}</div>
            </div>
            <el-empty v-if="!chunks.length" description="该文档没有 chunk" />
          </div>
          <el-pagination
            v-if="chunksTotal > chunkPageSize"
            small
            layout="prev, pager, next"
            :current-page="chunkPage"
            :page-size="chunkPageSize"
            :total="chunksTotal"
            @current-change="changeChunkPage"
          />
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
          <el-alert
            title="人工修改会立即同步检索索引；从原文件重建索引时会被覆盖。"
            type="warning"
            :closable="false"
            show-icon
            class="chunk-edit-warning"
          />
          <div class="chunk-detail-actions">
            <template v-if="editingChunk">
              <span>{{ chunkEditText.length }} / 20000 字符</span>
              <el-button @click="cancelChunkEdit" :disabled="chunkSaving">取消</el-button>
              <el-button type="primary" :loading="chunkSaving" @click="saveChunkEdit">保存并更新索引</el-button>
            </template>
            <el-button v-else type="primary" @click="startChunkEdit">编辑 chunk</el-button>
          </div>
          <div class="chunk-detail-text" :class="{ editing: editingChunk }">
            <el-input
              v-if="editingChunk"
              v-model="chunkEditText"
              type="textarea"
              :rows="18"
              :maxlength="20000"
              resize="none"
            />
            <pre v-else>{{ selectedChunk.text }}</pre>
          </div>
        </div>
        <el-empty v-else description="选择左侧 chunk 查看详情" />
      </div>
    </el-drawer>
  </el-container>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import DocumentUpload from "../../components/knowledge/DocumentUpload.vue";
import ModuleHeader from "../../components/shared/ModuleHeader.vue";
import { useKnowledgeBasesStore } from "../../stores/knowledgeBases";
import { useAuthStore } from "../../stores/auth";
import {
  deleteDocument as apiDeleteDocument,
  getDocChunks,
  getKnowledgeBaseDetail,
  getStats,
  listRetrievalTopics,
  reindex as apiReindex,
  reindexStatus,
  updateDocumentTopics,
  updateDocumentGovernance,
  updateDocChunk,
} from "../../api/knowledge/knowledgeBases";
import type { ChunkItem, DocumentItem, KnowledgeBase, RetrievalTopic, Stats } from "../../types";

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
const detailLoading = ref(false);
const documentSearch = ref("");
const documentStatusFilter = ref("all");
const governanceStatusFilter = ref("all");
const reindexingId = ref(""); // 正在重建的 doc_id 或 kb_id（按钮 loading）
const timers: number[] = [];

const filteredDetailDocs = computed(() => {
  const query = documentSearch.value.trim().toLowerCase();
  return detailDocs.value.filter((doc) => {
    const matchesQuery = !query || [doc.filename, doc.source_name, doc.content_owner]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(query));
    const matchesStatus = documentStatusFilter.value === "all" || doc.status === documentStatusFilter.value;
    const matchesGovernance = governanceStatusFilter.value === "all"
      || (governanceStatusFilter.value === "published"
        ? doc.governance_status === "published"
        : doc.governance_status !== "published");
    return matchesQuery && matchesStatus && matchesGovernance;
  });
});
const readyDocumentCount = computed(() => detailDocs.value.filter((doc) => doc.status === "ready").length);
const publishedDocumentCount = computed(
  () => detailDocs.value.filter((doc) => doc.governance_status === "published").length,
);

// chunk 明细
const showChunks = ref(false);
const chunks = ref<ChunkItem[]>([]);
const chunksTitle = ref("");
const chunksLoading = ref(false);
const selectedChunk = ref<ChunkItem | null>(null);
const selectedDocId = ref("");
const chunksTotal = ref(0);
const chunkPage = ref(1);
const chunkPageSize = 50;
const editingChunk = ref(false);
const chunkEditText = ref("");
const chunkSaving = ref(false);
const showTopics = ref(false);
const topicDocument = ref<DocumentItem | null>(null);
const availableTopics = ref<RetrievalTopic[]>([]);
const selectedTopicCodes = ref<string[]>([]);
const showGovernance = ref(false);
const governanceDocument = ref<DocumentItem | null>(null);
const governanceForm = ref({ sensitivity_level: "guest" as DocumentItem["sensitivity_level"], content_owner: "", source_name: "", effective_at: null as string | null, expires_at: null as string | null });

function openGovernance(row: DocumentItem) {
  governanceDocument.value = row;
  governanceForm.value = { sensitivity_level: row.sensitivity_level, content_owner: row.content_owner || "", source_name: row.source_name || row.filename, effective_at: row.effective_at, expires_at: row.expires_at };
  showGovernance.value = true;
}

async function saveGovernance() {
  const row = governanceDocument.value;
  if (!row || !governanceForm.value.content_owner.trim() || !governanceForm.value.source_name.trim()) return ElMessage.warning("请填写内容负责人和资料来源");
  try {
    await updateDocumentGovernance(detailKbId.value, row.doc_id, governanceForm.value);
    await refreshDetail(); showGovernance.value = false; ElMessage.success("治理信息已更新");
  } catch (error) { ElMessage.error((error as Error).message); }
}

function formatTime(t: string | undefined | null): string {
  if (!t) return "—";
  return new Date(t).toLocaleString("zh-CN", { hour12: false });
}

function documentExtension(filename: string): string {
  return filename.split(".").pop()?.slice(0, 4).toUpperCase() || "DOC";
}

function processingStatusLabel(status: string): string {
  return { ready: "已就绪", failed: "处理失败", processing: "处理中", reindexing: "重建中" }[status] || status;
}

function processingStatusType(status: string): "success" | "danger" | "warning" | "info" {
  if (status === "ready") return "success";
  if (status === "failed") return "danger";
  if (status === "processing" || status === "reindexing") return "warning";
  return "info";
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
  chunksTotal.value = 0;
  chunkPage.value = 1;
  editingChunk.value = false;
  try {
    const data = await getDocChunks(row.doc_id, 0, chunkPageSize);
    chunks.value = data.chunks;
    chunksTotal.value = data.total;
    chunksTitle.value = `${row.filename} 的 chunk（共 ${data.total} 块）`;
    if (data.chunks.length) selectedChunk.value = data.chunks[0];
  } catch (e) {
    ElMessage.error((e as Error).message);
  } finally {
    chunksLoading.value = false;
  }
}

function selectChunk(chunk: ChunkItem) {
  selectedChunk.value = chunk;
  editingChunk.value = false;
  chunkEditText.value = "";
}

async function changeChunkPage(page: number) {
  chunkPage.value = page;
  chunksLoading.value = true;
  editingChunk.value = false;
  try {
    const data = await getDocChunks(selectedDocId.value, (page - 1) * chunkPageSize, chunkPageSize);
    chunks.value = data.chunks;
    chunksTotal.value = data.total;
    selectedChunk.value = data.chunks[0] || null;
  } catch (error) {
    ElMessage.error((error as Error).message);
  } finally {
    chunksLoading.value = false;
  }
}

function startChunkEdit() {
  if (!selectedChunk.value) return;
  chunkEditText.value = selectedChunk.value.text;
  editingChunk.value = true;
}

function cancelChunkEdit() {
  editingChunk.value = false;
  chunkEditText.value = "";
}

async function saveChunkEdit() {
  const current = selectedChunk.value;
  const text = chunkEditText.value.trim();
  if (!current || !text) return ElMessage.warning("chunk 内容不能为空");
  if (text === current.text) return cancelChunkEdit();
  chunkSaving.value = true;
  try {
    const updated = await updateDocChunk(selectedDocId.value, current.chunk_index, text, current.updated_at);
    const index = chunks.value.findIndex((item) => item.chunk_index === updated.chunk_index);
    if (index >= 0) chunks.value[index] = updated;
    selectedChunk.value = updated;
    editingChunk.value = false;
    chunkEditText.value = "";
    ElMessage.success("chunk 已保存，向量与关键词索引已同步");
  } catch (error) {
    ElMessage.error((error as Error).message);
  } finally {
    chunkSaving.value = false;
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
  return { guest: "游客级", student: "学生级", editor: "实验室成员级", admin: "管理员级" }[level];
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
  detailKb.value = null;
  detailDocs.value = [];
  documentSearch.value = "";
  documentStatusFilter.value = "all";
  governanceStatusFilter.value = "all";
  showDetail.value = true;
  await refreshDetail();
}

function closeDetail() {
  showDetail.value = false;
  detailKbId.value = "";
  detailKb.value = null;
  detailDocs.value = [];
  detailError.value = "";
}

async function refreshDetail() {
  if (!detailKbId.value) return;
  detailLoading.value = true;
  try {
    const detail = await getKnowledgeBaseDetail(detailKbId.value);
    detailKb.value = { id: detail.id, name: detail.name, description: detail.description, embedding_model: detail.embedding_model, access_level: detail.access_level, document_count: detail.document_count, chunk_count: detail.chunk_count, created_at: detail.created_at };
    detailDocs.value = detail.documents;
    detailError.value = "";
  } catch (e) {
    detailError.value = (e as Error).message;
  } finally {
    detailLoading.value = false;
  }
}

async function handleDocumentUploaded() {
  await Promise.all([refreshDetail(), refreshStats(), kbStore.load()]);
}

function handleDocumentCommand(command: string, row: DocumentItem) {
  if (command === "reindex") void reindexDoc(row.doc_id);
  if (command === "delete") void confirmDeleteDoc(row);
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
  min-height: 100vh;
  background: #f5f7fb;
}
.kb-header {
  height: auto;
  min-height: 112px;
  padding: 22px 32px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  border-bottom: 1px solid var(--el-border-color-light);
  background: rgba(255, 255, 255, 0.96);
  box-sizing: border-box;
}
.kb-header.is-managing {
  min-height: 132px;
}
.header-eyebrow {
  display: block;
  margin-bottom: 7px;
  color: var(--el-color-primary);
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}
.kb-header h1 { margin: 0 0 7px; font-size: 26px; letter-spacing: -0.02em; }
.kb-header p { margin: 0; color: var(--el-text-color-secondary); font-size: 13px; }
.header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.header-actions .el-button + .el-button { margin-left: 0; }
.workspace-heading {
  display: flex;
  align-items: center;
  gap: 20px;
  min-width: 0;
}
.back-button {
  align-self: flex-start;
  margin-top: 2px;
}
.workspace-title-row {
  display: flex;
  align-items: center;
  gap: 10px;
}
.workspace-title-row h1 {
  max-width: 560px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.overview-main,
.workspace-main {
  padding: 24px 32px 40px;
  overflow: auto;
}
.stats-row {
  margin-bottom: 20px;
}
.stat {
  text-align: center;
  padding: 6px 0;
}
.stat-num {
  font-size: 26px;
  font-weight: 700;
}
.stat-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-top: 4px;
}
.kb-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
  gap: 18px;
}
.kb-card {
  height: 100%;
  border-radius: 12px;
  transition: transform 0.2s ease, box-shadow 0.2s ease;
}
.kb-card:hover {
  transform: translateY(-2px);
}
.kb-card-header {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.kb-name {
  font-weight: 600;
  margin-right: auto;
}
.kb-desc {
  font-size: 13px;
  color: var(--el-text-color-secondary);
  min-height: 40px;
  margin: 0 0 12px;
  line-height: 1.6;
}
.kb-stats {
  font-size: 13px;
  color: var(--el-text-color-secondary);
  margin-bottom: 14px;
}
.kb-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.kb-actions .el-button + .el-button { margin-left: 0; }
.workspace-summary {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 16px;
  margin-bottom: 18px;
}
.summary-tile {
  min-height: 116px;
  padding: 20px 22px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 14px;
  background: #fff;
  display: flex;
  flex-direction: column;
  box-sizing: border-box;
}
.summary-tile span {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.summary-tile strong {
  margin: 7px 0 3px;
  font-size: 30px;
  line-height: 1;
}
.summary-tile small {
  margin-top: auto;
  color: var(--el-text-color-placeholder);
}
.summary-tile-primary {
  color: #fff;
  border-color: transparent;
  background: linear-gradient(135deg, #3567e9, #7357df);
  box-shadow: 0 12px 28px rgba(62, 93, 210, 0.2);
}
.summary-tile-primary span,
.summary-tile-primary small {
  color: rgba(255, 255, 255, 0.78);
}
.upload-card,
.documents-card {
  border-radius: 14px;
  margin-bottom: 18px;
}
.panel-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 18px;
}
.panel-heading h2 {
  margin: 0 0 5px;
  font-size: 17px;
}
.panel-heading p {
  margin: 0;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.documents-heading {
  align-items: flex-end;
}
.document-filters {
  display: flex;
  align-items: center;
  gap: 10px;
}
.document-search { width: 280px; }
.status-filter { width: 160px; }
.detail-error { margin-bottom: 14px; }
.documents-table {
  width: 100%;
  --el-table-header-bg-color: #f8f9fc;
  --el-table-row-hover-bg-color: #f6f8ff;
}
.document-cell {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
  padding: 5px 0;
}
.document-icon {
  width: 42px;
  height: 42px;
  flex-shrink: 0;
  border-radius: 10px;
  display: grid;
  place-items: center;
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
  font-size: 10px;
  font-weight: 800;
}
.document-copy {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.document-copy strong,
.document-copy span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.document-copy strong { font-size: 14px; }
.document-copy span { color: var(--el-text-color-secondary); font-size: 12px; }
.chunk-count {
  color: var(--el-color-primary);
  font-size: 15px;
}
.topic-tags {
  display: flex;
  align-items: center;
  gap: 5px;
  flex-wrap: wrap;
}
.muted-text { color: var(--el-text-color-placeholder); font-size: 12px; }
.document-actions {
  display: flex;
  gap: 6px;
  align-items: center;
}
.document-actions .el-button + .el-button { margin-left: 0; }
:global(.danger-menu-item) { color: var(--el-color-danger); }
.upload-card :deep(.el-upload),
.upload-card :deep(.el-upload-dragger) {
  width: 100%;
}
.upload-card :deep(.el-upload-dragger) {
  padding: 22px;
  border-radius: 10px;
  background: #fafbff;
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
  gap: 22px;
  height: calc(100vh - 105px);
  padding: 0 4px 10px;
  box-sizing: border-box;
}
.chunk-nav-wrapper {
  width: 300px;
  flex-shrink: 0;
  border-right: 1px solid var(--el-border-color-light);
  padding-right: 14px;
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.chunk-nav {
  flex: 1;
  overflow-y: auto;
  min-height: 0;
}
.chunk-nav-wrapper .el-pagination {
  justify-content: center;
  padding-top: 8px;
}
.chunk-nav-item {
  padding: 12px 13px;
  border-radius: 9px;
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
  font-size: 13px;
  font-weight: 600;
  color: var(--el-color-primary);
}
.chunk-nav-preview {
  font-size: 13px;
  color: var(--el-text-color-secondary);
  margin-top: 5px;
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
.chunk-edit-warning {
  margin-bottom: 12px;
}
.chunk-detail-actions {
  min-height: 32px;
  margin-bottom: 10px;
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 8px;
}
.chunk-detail-actions span {
  margin-right: auto;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.chunk-detail-text {
  flex: 1;
  min-height: 0;
  border: 1px solid var(--el-border-color-light);
  border-radius: 8px;
  overflow: auto;
  background: var(--el-fill-color-lighter);
}
.chunk-detail-text.editing {
  border: 0;
  background: transparent;
  overflow: hidden;
}
.chunk-detail-text.editing :deep(.el-textarea),
.chunk-detail-text.editing :deep(.el-textarea__inner) {
  height: 100%;
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
@media (max-width: 1200px) {
  .workspace-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .documents-heading { align-items: flex-start; flex-direction: column; }
  .document-filters { width: 100%; }
  .document-search { flex: 1; width: auto; }
}
@media (max-width: 820px) {
  .kb-header,
  .kb-header.is-managing {
    align-items: flex-start;
    flex-direction: column;
    padding: 18px;
  }
  .header-actions { width: 100%; flex-wrap: wrap; }
  .workspace-heading { align-items: flex-start; flex-direction: column; gap: 10px; }
  .overview-main,
  .workspace-main { padding: 18px; }
  .stats-row :deep(.el-col-6) { max-width: 50%; flex: 0 0 50%; margin-bottom: 12px; }
  .workspace-summary { grid-template-columns: 1fr 1fr; }
  .document-filters { align-items: stretch; flex-direction: column; }
  .document-search,
  .status-filter { width: 100%; }
  .chunk-nav-wrapper { width: 210px; }
}
@media (max-width: 560px) {
  .workspace-summary { grid-template-columns: 1fr; }
  .panel-heading { align-items: flex-start; flex-direction: column; }
  .chunks-layout { gap: 10px; }
  .chunk-nav-wrapper { width: 150px; }
}
</style>
