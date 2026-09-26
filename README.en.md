# wechat-to-obsidian

English | [简体中文](README.md)

Save one public WeChat Official Account article, X Article, xAI News article, or ordinary webpage as an Obsidian note with localized body images, preserved tables and code blocks, and recoverable LaTeX math.

This repository is an Agent Skill (Codex/Claude skill package). The core is `scripts/capture_and_publish.py` — a zero-dependency publisher built entirely on the Python standard library. The Skill orchestrates invocation; the publisher performs deterministic fetching, parsing, and writing.

## Features

- Four source types: WeChat Official Account articles, `x.ai/news` articles, public X Articles, and ordinary HTTPS webpages.
- All body images are downloaded locally with references rewritten; avatars, QR codes, tracking pixels, and other page chrome are excluded automatically.
- Simple tables become Markdown tables; tables with merged cells become sanitized HTML tables.
- Code blocks keep their line breaks and indentation.
- WeChat `data-formula` and KaTeX/MathJax TeX sources are recovered as Obsidian `$...$` / `$$...$$` math; image-only formulas cannot be recovered as text.
- Strictly create-only: existing files are never overwritten; the note always keeps the originally submitted URL even when the server resolves it elsewhere.
- Emits only a compact result manifest (`publish_result.json`), never article content.

## Platform-specific validation (fail-closed)

| Source | Required markers | On failure |
| --- | --- | --- |
| WeChat | Browser-compatible request headers + real article content marker | Fails before creating any file when an access-challenge/verification page is detected |
| x.ai News | Page title marker + scoped `prose` body + `NewsArticle` JSON-LD | Fails when any marker is missing |
| X Article | Visible `x-article-body` marker + a complete article serialization (current inline `content_state` stream or legacy Draft.js) | Fails if any atomic block, media entity, or image URL cannot be mapped — no text-only fallback is published |

## Security Design

- Public HTTPS (port 443) URLs only; userinfo, local hostnames, and non-global IP literals are forbidden.
- Every resolved DNS address is validated as public, blocking SSRF/intranet probing.
- Size and count limits: 10MB HTML, 12MB per image, 80MB total images, max 60 images, 40 tables, 250 rows/table.
- Writes only within directories approved by the task specification; the script is the single entry point — no arguments, no shell expansion.
- Never copies source HTML attributes or scripts; never requests or stores cookies or credentials.

## Install as a Skill

```bash
CODEX_SKILLS_DIR="${CODEX_SKILLS_DIR:-$HOME/.codex/skills}"
git clone https://github.com/xieshentoken/wechat-to-obsidian.git \
  "${CODEX_SKILLS_DIR}/wechat-to-obsidian"
```

Python ≥ 3.10, zero third-party dependencies (standard library only).

## Changelog

- **v2.4.0** — Adapted to X's new RSC streaming article serialization (inline `content_state:$R[n]` inside `data-tsr-stream-part`): both the inline `content_state` and the legacy Draft.js serializations are supported, along with MEDIA / MARKDOWN / DIVIDER / TWEET entities and poster images for videos. See [`X_ARTICLE_FORMAT_FIX_PLAN.md`](X_ARTICLE_FORMAT_FIX_PLAN.md).

## Usage

This Skill runs under a sealed-task protocol: the caller (Watcher/orchestrator) writes the sealed task specification `.geof_cobs_task.json` into the current working directory, and the Skill then runs the publisher **exactly once**:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/capture_and_publish.py
```

Required specification fields:

```json
{
  "version": 1,
  "task_id": "<unique task id>",
  "skill_id": "wechat-to-obsidian",
  "source_url": "https://mp.weixin.qq.com/s/...",
  "vault_root": "/path/to/obsidian-vault",
  "inbox_dir": "/path/to/notes",
  "image_dir": "/path/to/images"
}
```

On success the note path, image count, and math count are returned in `publish_result.json` and stdout; on failure the exact error is reported without claiming a note was saved. Set the environment variable `GEOF_COBS_CANARY=1` to run canary (non-production-hardened) mode.

## Project Structure

```text
wechat-to-obsidian/
├── SKILL.md                        # Primary agent contract (invocation rules and boundaries)
├── README.md / README.en.md        # Bilingual readme
├── LICENSE                         # Apache License 2.0
└── scripts/
    └── capture_and_publish.py      # Zero-dependency publisher (fetch/parse/write/audit)
```

## License

Apache License 2.0 — see [LICENSE](LICENSE).
