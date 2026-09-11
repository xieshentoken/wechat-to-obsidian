# wechat-to-obsidian

[English](README.en.md) | 简体中文

将一篇公开的微信公众号文章、X Article、xAI News 文章或普通网页，保存为一条图片本地化、表格与代码块完整保留、LaTeX 公式可恢复的 Obsidian 笔记。

本仓库是一个 Agent Skill（Codex/Claude 技能包），核心是 `scripts/capture_and_publish.py` —— 一个仅依赖 Python 标准库的零依赖发布器。Skill 负责编排调用；发布器负责确定性的抓取、解析与落盘。

## 功能

- 支持 4 类来源：微信公众号文章、`x.ai/news` 文章、公开 X Articles、普通 HTTPS 网页。
- 正文图片全部下载到本地并重写引用；头像、二维码、追踪像素等页面杂项自动排除。
- 简单表格转为 Markdown 表格；含合并单元格的表格转为经清洗的 HTML 表格。
- 代码块保留换行与缩进。
- 微信 `data-formula` 与 KaTeX/MathJax 源恢复为 Obsidian `$...$` / `$$...$$` 数学；纯图片公式无法恢复为文本。
- 严格 create-only：绝不覆盖已有文件；笔记始终保留原始提交 URL，即使服务器最终跳转到其他地址。
- 只输出紧凑的结果清单（`publish_result.json`），不输出文章内容。

## 平台专属校验（fail-closed）

| 来源 | 必需标记 | 缺失时行为 |
| --- | --- | --- |
| 微信 | 浏览器兼容请求头 + 真实正文标记 | 遇到「环境异常/验证」页在创建任何文件前失败 |
| x.ai News | 页面标题标记 + `prose` 正文 + `NewsArticle` JSON-LD | 标记缺失即失败 |
| X Article | 可见 `x-article-body` 标记 + 序列化 Draft.js 数据 | 任一原子块/媒体实体/图片 URL 无法映射即失败，不发布纯文本降级版 |

## 安全设计

- 仅允许公网 HTTPS（443 端口）URL；禁止 userinfo、本地主机名、非全局 IP 字面量。
- DNS 解析结果逐一校验为公网地址，阻断 SSRF/内网探测。
- 大小与数量上限：HTML 10MB、单图 12MB、图片总量 80MB、最多 60 张图、40 张表、250 行/表。
- 只在任务规范批准的目录内写文件；脚本是唯一入口、无参数、无 shell 扩展。
- 不复制源 HTML 属性或脚本；不请求、不存储任何 Cookie 或凭据。

## 安装为 Skill

```bash
CODEX_SKILLS_DIR="${CODEX_SKILLS_DIR:-$HOME/.codex/skills}"
git clone https://github.com/xieshentoken/wechat-to-obsidian.git \
  "${CODEX_SKILLS_DIR}/wechat-to-obsidian"
```

Python ≥ 3.10，零第三方依赖（仅标准库）。

## 使用方式

本 Skill 按 sealed-task 协议运行：调用方（Watcher/编排器）在当前工作目录写入密封任务规范 `.geof_cobs_task.json`，Skill 随后运行发布器**恰好一次**：

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/capture_and_publish.py
```

任务规范必需字段：

```json
{
  "version": 1,
  "task_id": "<唯一任务 ID>",
  "skill_id": "wechat-to-obsidian",
  "source_url": "https://mp.weixin.qq.com/s/...",
  "vault_root": "/path/to/obsidian-vault",
  "inbox_dir": "/path/to/notes",
  "image_dir": "/path/to/images"
}
```

成功后在 `publish_result.json` 与 stdout 返回笔记路径、图片数与公式数；失败则返回精确错误，不声称已保存笔记。设置环境变量 `GEOF_COBS_CANARY=1` 可运行金丝雀（非生产强化）模式。

## 项目结构

```text
wechat-to-obsidian/
├── SKILL.md                        # Agent 主契约（调用规则与边界）
├── README.md / README.en.md        # 双语说明
├── LICENSE                         # Apache License 2.0
└── scripts/
    └── capture_and_publish.py      # 零依赖发布器（抓取/解析/落盘/审计）
```

## 许可证

Apache License 2.0 —— 见 [LICENSE](LICENSE)。
