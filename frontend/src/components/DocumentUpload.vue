<template>
  <div class="doc-upload">
    <el-upload
      drag
      multiple
      :auto-upload="true"
      :show-file-list="true"
      :http-request="doUpload"
      :before-upload="validate"
      accept=".pdf,.md,.txt,.docx"
    >
      <div class="upload-hint">📄 拖拽文档到此处，或<em>点击上传</em></div>
      <div class="upload-sub">支持 PDF / MD / TXT / DOCX，单个 ≤ 50MB</div>
    </el-upload>

    <!-- 上传中/处理中状态列表 -->
    <div v-for="item in uploading" :key="item.doc_id" class="upload-item">
      <el-icon class="is-loading"><Loading /></el-icon>
      <span class="upload-name">{{ item.filename }}</span>
      <el-tag size="small" :type="item.status === 'failed' ? 'danger' : 'primary'">
        {{ item.statusLabel }}
      </el-tag>
    </div>
    <div v-if="uploadError" class="upload-error">{{ uploadError }}</div>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, ref } from "vue";
import { ElMessage } from "element-plus";
import { Loading } from "@element-plus/icons-vue";
import { uploadDocument, type UploadResult } from "../api/knowledgeBases";
import type { UploadRequestOptions } from "element-plus";

const props = defineProps<{ kbId: string }>();
const emit = defineEmits<{ (e: "uploaded"): void }>();

const ALLOWED = ["pdf", "md", "txt", "docx"];
const MAX_SIZE = 50 * 1024 * 1024;

interface UploadingItem extends UploadResult {
  statusLabel: string;
}

const uploading = ref<UploadingItem[]>([]);
const uploadError = ref("");
const timers: number[] = [];

function validate(file: File): boolean {
  const ext = file.name.split(".").pop()?.toLowerCase() ?? "";
  if (!ALLOWED.includes(ext)) {
    ElMessage.error(`不支持 .${ext} 格式（支持 PDF/MD/TXT/DOCX）`);
    return false;
  }
  if (file.size > MAX_SIZE) {
    ElMessage.error("文件超过 50MB 限制");
    return false;
  }
  return true;
}

async function doUpload(options: UploadRequestOptions) {
  const file = options.file as File;
  uploadError.value = "";
  try {
    const result = await uploadDocument(props.kbId, file);
    const item: UploadingItem = { ...result, statusLabel: "上传中" };
    uploading.value.push(item);
    pollStatus(item);
    emit("uploaded");
  } catch (e) {
    uploadError.value = (e as Error).message;
    ElMessage.error((e as Error).message);
  }
}

/** 轮询文档处理状态（2s 间隔）直到非 processing；组件卸载时清理全部定时器 */
function pollStatus(item: UploadingItem) {
  const timer = window.setInterval(async () => {
    const resp = await fetch(`/api/knowledge-bases/${props.kbId}/documents`);
    const docs = await resp.json();
    const doc = docs.find((d: { doc_id: string }) => d.doc_id === item.doc_id);
    if (!doc) return;
    item.status = doc.status;
    item.statusLabel =
      doc.status === "ready"
        ? `完成（${doc.chunk_count} chunks）`
        : doc.status === "failed"
          ? "失败"
          : "处理中";
    if (doc.status === "failed") {
      uploadError.value = `${item.filename}: ${doc.error_message || "处理失败"}`;
      ElMessage.error(`${item.filename} 处理失败`);
      stopTimer(timer);
      emit("uploaded");
    } else if (doc.status === "ready") {
      stopTimer(timer);
      emit("uploaded");
    }
  }, 2000);
  timers.push(timer);
}

function stopTimer(timer: number) {
  window.clearInterval(timer);
  const idx = timers.indexOf(timer);
  if (idx >= 0) timers.splice(idx, 1);
}

onBeforeUnmount(() => timers.forEach((t) => window.clearInterval(t)));
</script>

<style scoped>
.upload-hint {
  font-size: 14px;
}
.upload-sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-top: 4px;
}
.upload-item {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
  font-size: 13px;
}
.upload-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.upload-error {
  color: var(--el-color-danger);
  font-size: 12px;
  margin-top: 8px;
}
</style>
