<template><section><h2>成员申请审批</h2><el-table :data="items" v-loading="loading"><el-table-column prop="nickname" label="申请人" /><el-table-column prop="reason" label="申请理由" min-width="280" /><el-table-column prop="created_at" label="申请时间" width="190" /><el-table-column label="操作" width="150"><template #default="{row}"><el-button link type="success" @click="review(row,true)">批准</el-button><el-button link type="danger" @click="review(row,false)">驳回</el-button></template></el-table-column></el-table></section></template>
<script setup lang="ts">
import { onMounted, ref } from "vue"; import { ElMessage, ElMessageBox } from "element-plus";
import { listApplications, reviewApplication } from "../../../api/identity/roleApplications"; import type { RoleApplication } from "../../../types";
const items=ref<RoleApplication[]>([]); const loading=ref(false);
async function load(){loading.value=true;try{items.value=(await listApplications()).items;}finally{loading.value=false;}}
async function review(row:RoleApplication,approve:boolean){try{const {value}=await ElMessageBox.prompt(approve?"可填写审批意见":"请填写驳回原因","审批",{inputValidator:(v)=>approve||!!v.trim()||"驳回原因不能为空"});await reviewApplication(row.id,approve,value);ElMessage.success("审批完成");await load();}catch{/* 取消 */}}
onMounted(load);
</script>
