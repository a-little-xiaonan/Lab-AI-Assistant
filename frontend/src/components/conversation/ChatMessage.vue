<template>
  <div class="msg" :class="message.role">
    <div class="bubble">
      <div v-if="message.role === 'user'" class="user-text">{{ message.content }}</div>
      <div v-else class="md-body" v-html="rendered" />
      <div v-if="message.streaming" class="cursor">▋</div>
      <div v-if="message.error" class="msg-error">{{ message.error }}</div>
      <AnswerFeedback v-if="message.role === 'assistant' && message.id && !message.streaming" :message-id="message.id" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import DOMPurify from "dompurify";
import { marked } from "marked";
import hljs from "highlight.js";
import "highlight.js/styles/github.css";
import type { ChatMessage } from "../../stores/session";
import AnswerFeedback from "./AnswerFeedback.vue";

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
renderer.link = ({ href, title, text }: { href: string; title?: string | null; text: string }) => {
  // 外部工具可能带回链接；只允许 HTTP(S)，并隔离新页面的 opener。
  if (!/^https?:\/\//i.test(href)) return text;
  const escapedTitle = title ? ` title="${title.replace(/"/g, "&quot;")}"` : "";
  return `<a href="${href}"${escapedTitle} target="_blank" rel="noopener noreferrer nofollow">${text}</a>`;
};
marked.use({ renderer, breaks: true });

const rendered = computed(() =>
  DOMPurify.sanitize(marked.parse(props.message.content || "") as string, {
    ALLOWED_TAGS: ["a", "p", "br", "strong", "em", "del", "code", "pre", "blockquote", "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "table", "thead", "tbody", "tr", "th", "td", "span"],
    ALLOWED_ATTR: ["href", "title", "target", "rel", "class"],
  }),
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
