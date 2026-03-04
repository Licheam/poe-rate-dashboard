# Poe Rate Dashboard

一个基于 **FastAPI + Chart.js** 的 Poe 模型费率可视化面板。

它会抓取 `models_config.toml` 中配置的模型费率数据，保存到 `static/data.json`，并在网页中展示图表和表格对比。

## 功能

- 一键更新模型费率数据
- 图表展示输入/输出价格（USD / 1M tokens）
- 表格搜索与排序
- 支持在页面中增删模型配置
- 支持临时禁用模型（仅浏览器本地生效）

## 技术栈

- 后端：FastAPI、Uvicorn、httpx、toml
- 前端：原生 HTML/CSS/JS + Chart.js

## 快速开始

### 1. 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install fastapi uvicorn httpx toml
```

### 2. 启动服务

```bash
python server.py
```

默认访问地址：

- http://127.0.0.1:8000

## 配置模型

项目使用 `models_config.toml` 管理要抓取的模型列表：

```toml
handles = ["Claude-Opus-4.6", "GPT-5.2"]
```

你也可以在页面里直接添加/删除模型。

## 常用 API

- `GET /api/config`：获取模型配置
- `POST /api/config`：新增模型（body: `{ "handle": "xxx" }`）
- `DELETE /api/config/{handle}`：删除模型
- `GET /api/update`：更新全部或指定模型数据
- `GET /api/update/status`：获取更新进度
- `GET /api/data`：读取当前已保存数据

## 目录结构

```text
.
├── server.py               # FastAPI 服务与抓取逻辑
├── models_config.toml      # 模型列表配置
└── static
    ├── index.html          # 页面入口
    ├── css/style.css       # 样式
    ├── js/app.js           # 前端逻辑
    └── data.json           # 抓取结果（运行后生成/更新）
```

## 说明

- 抓取依赖 Poe 页面与接口结构，若 Poe 变更可能导致抓取失败。
- 本项目主要用于个人对比和可视化，不保证数据实时性与稳定性。

## License

This project is licensed under the [MIT License](LICENSE).