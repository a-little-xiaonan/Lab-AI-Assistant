<template>
  <div class="input-bar">
    <el-input
      v-model="text"
      type="textarea"
      :rows="2"
      :disabled="disabled || streaming"
      placeholder="输入问题，Enter 发送，Shift+Enter 换行"
      @keydown.enter.exact.prevent="send"
    />
    <div class="actions">
      <el-button v-if="streaming" type="danger" @click="$emit('stop')">■ 停止生成</el-button>
      <el-button
        v-else
        type="primary"
        :disabled="disabled || !text.trim()"
        @click="send"
      >
        发送
      </el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from "vue";

defineProps<{
  disabled: boolean;
  streaming: boolean;
}>();
const emit = defineEmits<{
  (e: "send", text: string): void;
  (e: "stop"): void;
}>();

const text = ref("");

function send() {
  const t = text.value.trim();
  if (!t) return;
  emit("send", t);
  text.value = "";
}
</script>

<style scoped>
.input-bar {
  padding: 12px 24px 20px;
  border-top: 1px solid var(--el-border-color-light);
}
.actions {
  display: flex;
  justify-content: flex-end;
  margin-top: 8px;
}
</style>
