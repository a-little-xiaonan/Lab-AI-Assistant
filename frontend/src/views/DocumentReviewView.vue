<template>
  <el-container class="review-page" direction="vertical">
    <ModuleHeader section="knowledge" />
    <el-header class="review-header">
      <div><h1>文档审核工作台</h1><p>草稿分块经质量检查和人工审核后，才进入正式检索索引</p></div>
      <el-button @click="$router.push('/knowledge-bases')">返回知识库</el-button>
    </el-header>
    <el-main>
      <el-table :data="rows" v-loading="loading" stripe>
        <el-table-column prop="filename" label="文档" min-width="220" />
        <el-table-column prop="version_no" label="版本" width="80" />
        <el-table-column prop="chunk_count" label="分块" width="80" />
        <el-table-column prop="review_status" label="审核状态" width="130" />
        <el-table-column label="操作" width="260">
          <template #default="{ row }">
            <el-button link @click="openDetail(row)">检查</el-button>
            <el-button link type="primary" @click="act(row, 'preapprove')">预审通过</el-button>
            <el-button v-if="auth.isAdmin" link type="success" @click="act(row, 'publish')">发布</el-button>
            <el-button link type="danger" @click="act(row, auth.isAdmin ? 'reject' : 'request-change')">退回</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-main>
    <el-drawer v-model="showDetail" :title="detail?.filename || '审核详情'" size="75%">
      <template v-if="detail">
        <h3>质量检查</h3>
        <el-table :data="detail.quality_checks" size="small">
          <el-table-column prop="check_type" label="检查项" />
          <el-table-column prop="severity" label="级别" width="100" />
          <el-table-column prop="result" label="结果" width="100" />
          <el-table-column label="说明"><template #default="{ row }">{{ row.details.message || '—' }}</template></el-table-column>
        </el-table>
        <h3>待发布内容</h3>
        <el-collapse>
          <el-collapse-item v-for="chunk in detail.chunks" :key="chunk.chunk_index" :title="`Chunk #${chunk.chunk_index} · ${chunk.char_length} 字符`">
            <pre class="chunk-text">{{ chunk.text }}</pre>
          </el-collapse-item>
        </el-collapse>
        <h3>审核记录</h3>
        <el-timeline>
          <el-timeline-item v-for="item in detail.reviews" :key="item.created_at" :timestamp="new Date(item.created_at).toLocaleString()">
            {{ item.action }} {{ item.comment || '' }}
          </el-timeline-item>
        </el-timeline>
      </template>
    </el-drawer>
  </el-container>
</template>

<script setup lang="ts">
import { onMounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import ModuleHeader from "../components/ModuleHeader.vue";
import { getDocumentReviewDetail, listDocumentReviews, reviewDocument } from "../api/knowledgeBases";
import { useAuthStore } from "../stores/auth";
import type { ReviewDetail, ReviewSummary } from "../types";

const auth = useAuthStore();
const rows = ref<ReviewSummary[]>([]);
const loading = ref(false);
const showDetail = ref(false);
const detail = ref<ReviewDetail | null>(null);

async function load() { loading.value = true; try { rows.value = await listDocumentReviews(); } finally { loading.value = false; } }
async function openDetail(row: ReviewSummary) { detail.value = await getDocumentReviewDetail(row.doc_id); showDetail.value = true; }
async function act(row: ReviewSummary, action: "preapprove" | "publish" | "reject" | "request-change") {
  try {
    const { value } = await ElMessageBox.prompt("可填写审核意见", "确认操作", { inputType: "textarea", inputPlaceholder: "审核意见（可选）" });
    await reviewDocument(row.doc_id, action, row.lock_version, value);
    ElMessage.success(action === "publish" ? "发布任务已提交" : "审核状态已更新");
    await load();
  } catch { /* 用户取消或接口错误由下一次刷新呈现 */ }
}
onMounted(load);
</script>

<style scoped>
.review-page { height: 100%; }
.review-header { height: auto; padding: 18px 28px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--el-border-color-light); }
.review-header h1 { margin: 0 0 6px; font-size: 22px; }
.review-header p { margin: 0; color: var(--el-text-color-secondary); }
.chunk-text { white-space: pre-wrap; line-height: 1.7; font-family: inherit; }
</style>
