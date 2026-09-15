<template>
  <section class="evaluation-page">
    <div class="page-heading">
      <div><h2>RAG 量化评测</h2><p>固定数据集、知识库快照和配置，持续比较检索与回答质量。</p></div>
      <el-button :loading="loading" @click="load">刷新</el-button>
    </div>
    <el-card shadow="never" class="actions">
      <el-select v-model="split" style="width: 150px"><el-option label="开发集" value="dev"/><el-option label="保留集" value="holdout"/><el-option label="全部" value="all"/></el-select>
      <el-button type="primary" :loading="starting" @click="run('retrieval')">运行检索评测</el-button>
      <el-button :loading="starting" @click="run('full')">运行完整问答评测</el-button>
      <span class="hint">完整评测会调用对话模型；保留集用于最终验收，不建议频繁调参。</span>
    </el-card>
    <el-table :data="items" v-loading="loading" class="run-table">
      <el-table-column prop="id" label="运行 ID" min-width="220"/>
      <el-table-column label="模式/分区" width="140"><template #default="{ row }">{{ row.mode }} / {{ row.dataset_split }}</template></el-table-column>
      <el-table-column label="状态" width="100"><template #default="{ row }"><el-tag :type="statusType(row.status)">{{ statusText(row.status) }}</el-tag></template></el-table-column>
      <el-table-column label="Recall@5" width="110"><template #default="{ row }">{{ percent(row.metrics.retrieval_recall_at_5) }}</template></el-table-column>
      <el-table-column label="MRR" width="90"><template #default="{ row }">{{ decimal(row.metrics.mrr) }}</template></el-table-column>
      <el-table-column label="nDCG@5" width="100"><template #default="{ row }">{{ decimal(row.metrics.ndcg_at_5) }}</template></el-table-column>
      <el-table-column label="证据门槛" width="110"><template #default="{ row }">{{ percent(row.metrics.evidence_gate_accuracy) }}</template></el-table-column>
      <el-table-column label="P95" width="90"><template #default="{ row }">{{ seconds(row.metrics.end_to_end_p95_seconds ?? row.metrics.retrieval_p95_seconds) }}</template></el-table-column>
      <el-table-column prop="started_at" label="开始时间" min-width="170"/>
      <el-table-column prop="error_message" label="异常" min-width="180" show-overflow-tooltip/>
    </el-table>
  </section>
</template>

<script setup lang="ts">
import { ElMessage, ElMessageBox } from "element-plus";
import { onMounted, ref } from "vue";
import { listEvaluationRuns, startEvaluation } from "../api/evaluation";
import type { EvaluationRunItem } from "../types";

const items = ref<EvaluationRunItem[]>([]);
const split = ref<"dev" | "holdout" | "all">("dev");
const loading = ref(false);
const starting = ref(false);

function percent(value: unknown) { return typeof value === "number" ? `${(value * 100).toFixed(1)}%` : "-"; }
function decimal(value: unknown) { return typeof value === "number" ? value.toFixed(3) : "-"; }
function seconds(value: unknown) { return typeof value === "number" ? `${value.toFixed(2)}s` : "-"; }
function statusText(value: string) { return ({ running: "运行中", completed: "已完成", failed: "失败" } as Record<string, string>)[value] ?? value; }
function statusType(value: string) { return value === "completed" ? "success" : value === "failed" ? "danger" : "warning"; }
async function load() { loading.value = true; try { items.value = (await listEvaluationRuns()).items; } catch (e) { ElMessage.error(String(e)); } finally { loading.value = false; } }
async function run(mode: "retrieval" | "full") {
  if (split.value === "holdout" || split.value === "all") await ElMessageBox.confirm("保留集应只用于阶段验收，确认运行？", "评测确认", { type: "warning" });
  starting.value = true;
  try { const result = await startEvaluation(mode, split.value); ElMessage.success(`已创建评测：${result.run_id}`); setTimeout(load, 1000); }
  catch (e) { ElMessage.error(String(e)); } finally { starting.value = false; }
}
onMounted(load);
</script>

<style scoped>
.evaluation-page { display: flex; flex-direction: column; gap: 18px; }
.page-heading { display: flex; align-items: center; justify-content: space-between; }
.page-heading h2 { margin: 0 0 6px; }.page-heading p { margin: 0; color: var(--el-text-color-secondary); }
.actions :deep(.el-card__body) { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.hint { color: var(--el-text-color-secondary); font-size: 12px; }.run-table { border-radius: 8px; }
</style>
