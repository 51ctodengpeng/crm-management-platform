# CRM-code

客户关系管理（CRM）系统代码仓库。

## 项目简介

本仓库用于存放 CRM 系统的源代码与相关文档。当前处于初始化阶段，后续按模块补充。

## 目录结构

```
CRM-code/
├── README.md
├── .gitignore
└── （待补充：src / docs / deploy 等目录）
```

## 环境要求

| 组件 | 版本 | 说明 |
|---|---|---|
| Git | 2.40+ | 版本控制 |
| 运行时 | 待补充 | 如 .NET 8 / Node.js 20 / JDK 17 |
| 数据库 | 待补充 | 如 SQL Server 2019 / MySQL 8.0 |

## 本地运行

```bash
# 1. 获取代码
git clone https://github.com/51ctodengpeng/CRM-code.git
cd CRM-code

# 2. 安装依赖
# （待补充：dotnet restore / npm install / mvn dependency:go-offline）

# 3. 配置环境变量
# 复制 .env.example 为 .env 并填写本地配置

# 4. 启动
# （待补充：启动命令）
```

## 分支约定

| 分支 | 用途 |
|---|---|
| `main` | 稳定分支，可发布 |
| `feature/<名称>` | 功能开发 |
| `fix/<名称>` | 缺陷修复 |

提交信息格式：`<类型>: <说明>`，类型取 `feat` / `fix` / `docs` / `refactor` / `chore`。

## 安全约定

- 禁止提交任何账号密码、密钥、连接字符串、`.env` 文件
- 生产配置一律通过环境变量或密钥管理服务注入
- 如误提交敏感信息，删除文件无效，须清理 Git 历史并立即吊销凭证

## 许可证

待定。
