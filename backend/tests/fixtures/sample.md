# 千问大模型产品手册（示例文档，用于测试 RAG 链路）

## 产品概述

千问大模型是阿里云通义千问系列的最新大语言模型，提供对话、写作、代码生成、数据分析等能力。千问系列包括 qwen-turbo、qwen-plus、qwen-max 等多个版本，满足不同场景的需求。

## 定价方案

千问大模型的定价按 token 计费。qwen-plus 的输入价格为 0.0008 元/千 token，输出价格为 0.002 元/千 token。qwen-max 的输入价格为 0.004 元/千 token，输出价格为 0.012 元/千 token。API 调用按量付费，不收取月费。

调用 API 需要先开通 DashScope 服务并创建 API Key。API Key 以 sk- 开头，在代码中通过环境变量 DASHSCOPE_API_KEY 传入。

## 上下文窗口

qwen-plus 支持 128K 的上下文窗口，可以处理长文档输入。qwen-max 支持 32K 上下文窗口。

## 常见问题

问：如何开通千问 API？答：访问阿里云百炼控制台，开通模型服务后创建 API Key。

问：支持流式输出吗？答：支持，通过 SSE 协议实现。

问：embedding 模型是什么？答：text-embedding-v3，输出 1024 维向量。
