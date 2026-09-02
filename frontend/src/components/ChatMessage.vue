<template>
  <div class="msg" :class="message.role">
    <div class="bubble">
      <div v-if="message.role === 'user'" class="user-text">{{ message.content }}</div>
      <div v-else class="md-body" v-html="rendered" />
      <div v-if="message.streaming" class="cursor">▋</div>
      <div v-if="message.error" class="msg-error">{{ message.error }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { marked } from "marked";
import hljs from "highlight.js";
import "highlight.js/styles/github.css";
import type { ChatMessage } from "../stores/session";

const props = defineProps<{ message: ChatMessage }>();

// marked v15 不再内置 highlight：自定义 renderer 做代码高亮
const renderer = new marked.Renderer();
renderer.code = ({ text, lang }: { text: string; lang?: string }) => {
  const language = lang && hljs.getLanguage(lang) ? lang : "";
  const highlighted = language
    ? hljs.highlight(text, { language }).value
    : hljs.highlightAuto(text).value;
  return `<pre><code class="hljs language-${language}">${highlighted}</code></pre>`;
};
marked.use({ renderer, breaks: true });

const rendered = computed(() =>
  marked.parse(props.message.content || "") as string,
);
</script>

<style scoped>
.msg {
  display: flex;
  margin-bottom: 16px;
}
.msg.user {
  justify-content: flex-end;
}
.msg.assistant {
  justify-content: flex-start;
}
.bubble {
  max-width: 78%;
  padding: 10px 14px;
  border-radius: 10px;
  line-height: 1.7;
  font-size: 14px;
}
.msg.user .bubble {
  background: var(--el-color-primary-light-8);
}
.msg.assistant .bubble {
  background: var(--el-fill-color-light);
}
.user-text {
  white-space: pre-wrap;
  word-break: break-word;
}
.cursor {
  display: inline-block;
  animation: blink 1s infinite;
  color: var(--el-color-primary);
}
@keyframes blink {
  50% {
    opacity: 0;
  }
}
.msg-error {
  color: var(--el-color-danger);
  font-size: 13px;
  margin-top: 6px;
}
:deep(.md-body pre) {
  background: #f6f8fa;
  padding: 10px;
  border-radius: 6px;
  overflow-x: auto;
}
:deep(.md-body code) {
  font-family: "SF Mono", Consolas, monospace;
  font-size: 13px;
}
</style>
