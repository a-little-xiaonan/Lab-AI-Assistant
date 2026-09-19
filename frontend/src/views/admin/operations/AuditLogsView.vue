<template><section><h2>审计日志</h2><el-form inline><el-form-item label="操作"><el-input v-model="action" clearable /></el-form-item><el-form-item label="Request ID"><el-input v-model="requestId" clearable /></el-form-item><el-button type="primary" @click="load">查询</el-button></el-form><el-table :data="items"><el-table-column prop="created_at" label="时间" width="190"/><el-table-column prop="actor_role" label="角色" width="100"/><el-table-column prop="action" label="操作"/><el-table-column prop="resource_type" label="资源"/><el-table-column prop="resource_id" label="资源 ID"/><el-table-column prop="result" label="结果" width="90"/><el-table-column prop="request_id" label="Request ID" min-width="180"/></el-table><el-pagination v-model:current-page="page" :page-size="20" :total="total" @current-change="load"/></section></template>
<script setup lang="ts">
import { onMounted, ref } from "vue"; import { listAuditLogs } from "../../../api/operations/audit"; import type { AuditLogItem } from "../../../types";
const items=ref<AuditLogItem[]>([]),total=ref(0),page=ref(1),action=ref(""),requestId=ref("");
async function load(){const r=await listAuditLogs({offset:(page.value-1)*20,limit:20,...(action.value?{action:action.value}:{}),...(requestId.value?{request_id:requestId.value}:{})});items.value=r.items;total.value=r.total;}
onMounted(load);
</script>
