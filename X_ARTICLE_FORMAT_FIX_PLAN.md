# wechat-to-obsidian v2.3.0 → v2.4.0 修改方案：适配 x.com 新文章序列化格式

> 状态：已落地为 v2.4.0（2026-09-26）。旧失败任务元数据仍绑定 2.3.0，需作为新任务重投，不能直接 retry。
> 影响范围：仅 `scripts/capture_and_publish.py` 的 X Article 解析路径 + `SKILL.md` 描述 + skill 注册表
> 微信 / xAI / 普通网页路径：零改动

---

## 1. 问题定位（已复核确认）

- 抓取正常（HTTP 200，`x-article-body` 标记存在），但页面序列化格式已更换：
  - 旧格式：`__typename:"DraftJsContentState"` / `DraftJsBlock` / `DraftJsEntityMap` / `ArticleMediaKey` 等 Draft.js 类型标记（v2.3.0 解析器唯一支持的格式）
  - 新格式：React Server Components 流式序列化（`data-tsr-stream-part` script 内），全部为 `$R[n]={...}` 引用式对象，无任何 `DraftJs*` 类型标记
- `x_article_stream()` 因找不到 `DraftJsContentState` 直接抛错 → fail-closed 中止 → 无 `publish_result.json` → watcher 报 "Claude Skill did not produce the sealed publish manifest"
- 归档中的失败任务全部是 x.com 链接（v2.3.0），非偶发，是格式兼容性问题

## 2. 新格式技术规格（从 4 个真实样本逆向验证）

### 2.1 数据位置与整体结构

数据在 `<script data-tsr-stream-part>` 内，主文章的 ArticleEntity 结构（键按字母序排列）：

```
result:$R[n]={
  __isArticleResult:"ArticleEntity",
  __typename:"ArticleEntity",
  content_state:$R[a]={
    blocks:$R[b]=[
      $R[x]={data:$R[..]={},entity_ranges:$R[..]=[$R[..]={key:0,length:1,offset:0}],
             inline_style_ranges:$R[..]=[],key:"9u1rq",text:"...",type:"unstyled"},
      ...
    ],
    entity_map:$R[c]=[
      $R[y]={key:"4",value:$R[z]={data:$R[w]={markdown:"```markdown\n...```"},type:"MARKDOWN"}},
      $R[y]={key:"1",value:$R[z]={data:$R[w]={media_items:$R[v]=[$R[u]={media_id:"..."}]},type:"MEDIA"}},
      ...
    ]
  },
  cover_media_results:$R[..]={...,result:$R[..]={__typename:"ApiMedia",...,media_info:$R[..]={__typename:"ApiImage",original_img_height:H,original_img_url:"https://...",original_img_width:W}}},
  id:"...",media_entities:$R[..]=[
    $R[m]={id:"...",media_id:"...",media_info:$R[..]={__typename:"ApiImage"|"ApiVideo",...original_img_url...}},
  ],
  metadata:$R[..]={created_at_secs,first_published_at_secs,modified_at_secs},
  preview_text:"...",rest_id:"...",title:"..."
}
```

### 2.2 与旧格式的关键差异

| 项目 | 旧格式（Draft.js） | 新格式（content_state $R） |
|---|---|---|
| 流定位标记 | `__id:"...:content_state",__typename:"DraftJsContentState"` | `content_state:$R[n]={blocks:$R[m]=[` |
| 块结构 | `__id/__typename/key/text/type` 分散引用 | 内联对象，键序固定：data,entity_ranges,inline_style_ranges,key,text,type |
| 块顺序 | `state_id:blocks:N"` 索引标签（校验 0..N-1 连续） | 数组顺序即文档顺序，无索引 |
| 实体 key | entity_map 中字符串 / 块引用处数字 | 同（entity_map `key:"4"` 字符串，块内 `{key:4,...}` 数字） |
| 实体类型 | 只认 MEDIA | **MEDIA / MARKDOWN / LINK / DIVIDER / TWEET**（4 样本实测） |
| 正文图片映射 | `ApiMedia` 段扫描 media_id+original_img_url | `media_entities` 数组：`media_id:"..."` → `media_info.original_img_url` |
| 视频 | （极少） | `media_info:{__typename:"ApiVideo",preview_image:{original_img_url:...},variants:[...]}`，取 poster 图（与旧段扫描行为等价） |
| 标题 | `__typename:"ArticleEntity",title:` 相邻 | `rest_id:"...",title:` 相邻（title 为对象最后一个键） |
| 封面 | DOM `img[alt="article cover image"]` | 流内 `cover_media_results`（原图 URL）；DOM alt 仍在（现为 `Article cover image`，casefold 匹配仍命中） |

### 2.3 实测样本验证结果（原型解析器，4/4 成功）

| 样本 | 块数 | 图片 | 用到的实体类型 | 备注 |
|---|---|---|---|---|
| 样本 A | 22 | 4 | MEDIA、MARKDOWN | 含 markdown 卡片 |
| 样本 B | 272 | 25 | MEDIA、MARKDOWN、DIVIDER、TWEET | 含视频(取poster)、18个分隔线、2个嵌入推文 |
| 样本 C | 516 | 19 | MEDIA、DIVIDER、TWEET | 54 个内联 LINK（旧解析器同样忽略，维持行为） |
| 样本 D | 84 | 7 | MEDIA、MARKDOWN | — |

块类型实测：unstyled、header-one/two、atomic、unordered/ordered-list-item、blockquote（code-block 保留旧逻辑但样本中未出现）。

## 3. 代码修改设计（scripts/capture_and_publish.py）

### 3.1 改动总览（函数级）

```
不变：has_x_article_body、x_article_cover_url、decode_js_string、is_x_status_url、
      extract_article 分发逻辑、微信/xAI/普通网页全部路径、HTTP/下载/写盘
微调：x_article_stream（候选 script 判定条件 + 错误信息）
重构：extract_x_article（拆为：分发器 + 旧格式函数 + 新格式函数 + 共享渲染 helper）
新增：extract_x_article_content_state（新格式解析）及子步骤
文档：SKILL.md X Article 段落
```

### 3.2 `x_article_stream(root)`（微调）

- 候选 script 判定：包含 `__typename:"ArticleEntity"` 且（`__typename:"DraftJsContentState"` 或 `content_state:$R[`）
- 保留现有择优启发式 `max(candidates, key=(original_img_url 计数, 长度))`
- 找不到时的错误信息改为同时提及两种序列化，例如：
  `"X Article content data is missing (neither Draft.js nor content_state serialization); refusing to publish a text-only fallback"`

### 3.3 `extract_x_article(root, source_url)`（重构为分发器）

1. `has_x_article_body` 检查不变（fail-closed 前置）
2. `stream = x_article_stream(root)`
3. 分发：
   - `re.search(r'content_state:\$R\[\d+\]=\{blocks:', stream)` 命中 → 新格式 `extract_x_article_content_state(...)`
   - 否则 `DraftJsContentState` 命中 → 旧格式 `extract_x_article_draftjs(...)`（即现 v2.3.0 逻辑原样保留，含全部 fail-closed 校验）
   - 两者皆无 → 抛错（新错误信息）
4. `article_marker` 区分：新格式 `"x:article+content_state"`，旧格式 `"x:article+draftjs"`（便于线上观测解析路径）

> 保留旧格式路径的原因：解析器按 fail-closed 设计，双格式支持使 x.com 回滚或灰度期间均可用；旧路径是被生产验证过的代码，不重构只搬运。

### 3.4 `extract_x_article_content_state(stream, root, source_url)`（新增，核心）

以下正则均已在 4 个真实样本上通过连续消费式解析验证（任何一处结构不符即抛 `CaptureError`，绝不静默降级）：

**Step 1 — 块数组连续解析（fail-closed 完整性校验）**

```
锚点: content_state:\$R\[\d+\]=\{blocks:\$R\[\d+\]=\[
块:   \$R\[\d+\]=\{data:\$R\[\d+\]=\{(?:[^{}]|\{[^{}]*\})*\},        # data 可含 mentions/urls 一层嵌套
      entity_ranges:\$R\[\d+\]=\[(?:\$R\[\d+\]=\{key:(\d+),length:\d+,offset:\d+\},?)*\],
      (?:depth:\d+,)?                                               # 防御未来字段
      inline_style_ranges:\$R\[\d+\]=\[(?:\$R\[\d+\]=\{length:\d+,offset:\d+,style:"[^"]+"\},?)*\],
      key:(JS_STRING),text:(JS_STRING),type:(JS_STRING)\}
终止: ],entity_map:    —— 逐块 re.match(content, pos) 推进，遇到既非块又非终止符 → "X Article block sequence is malformed"
数量: > MAX_X_DRAFT_BLOCKS(1000) → 抛错（不变）
```

**Step 2 — entity_map 解析**

```
锚点: \],entity_map:\$R\[\d+\]=\[
条目: \$R\[\d+\]=\{key:(JS_STRING),value:\$R\[\d+\]=\{data:\$R\[\d+\]=\{(?:[^{}]|\{[^{}]*\})*\},type:(JS_STRING)\}\}
终止: ]},    （entity_map 为 content_state 最后一个键，其后必跟 ArticleEntity 下一键）
按 type 分发收集（key 须为纯数字字符串，同旧校验）：
  MEDIA    → data 段内 findall media_id:"(\d+)"；无 → "media entity has no media identifier"
  MARKDOWN → data 段内 markdown:(JS_STRING)
  DIVIDER  → 无数据
  TWEET    → data 段内 tweet_id:"(\d+)"
  LINK     → data 段内 url:(JS_STRING)（阶段一不内联渲染，见 §5）
  其他类型 → 登记为 unknown；仅当被 atomic 块引用时才抛错（对齐旧 "unsupported entity" 语义）
```

**Step 3 — 标题 + 解析区域边界**

```
锚点: rest_id:"(\d+)",title:(JS_STRING)   —— 从 content_state 锚点向后首个命中即主文章标题
  （键按字母序，title 是 ArticleEntity 最后一个键；title 缺失 → "metadata or content state is incomplete"，同旧语义）
region_end = 该命中结束位置 → 后续 cover/media 扫描全部限定在 [content_state 起点, region_end] 内，
  防止串到页面中后续序列化的嵌入文章/推荐文章对象（样本中存在多个 ArticleEntity）
```

**Step 4 — 封面**

```
首选: region 内 cover_media_results:...media_info:...original_img_url:(JS_STRING)（原图 URL）
回退: 现有 x_article_cover_url()（DOM img alt="article cover image"，casefold 匹配对新 DOM 仍有效）
```

**Step 5 — 正文图片映射（media_entities 段扫描，容错语义与旧 ApiMedia 段扫描一致）**

```
锚点: region 内 media_entities:\$R\[\d+\]=\[
条目起点: \$R\[\d+\]=\{id:
条目段 = 到下一 \{id: 条目起点或 region_end，截断 4096 字符（对齐旧解析器容错上限）
段内 media_id:"(\d+)" + 首个 original_img_url:(JS_STRING) 同时存在才登记
  → 天然覆盖 ApiImage（直接 URL）与 ApiVideo（poster URL），行为与旧段扫描等价
```

**Step 6 — 块渲染（类型映射与旧格式完全一致，抽共享 helper）**

```
共享 helper: render_x_text_block(block_type, block_text) -> str
  header-one..six → #..######、unordered/ordered-list-item、blockquote、code-block（围栏加固逻辑不变）、其余按正文
新格式 atomic 块（entity_ranges 数字 key 逐个映射）：
  MEDIA    → 每个 media_id 查 media_urls；缺失 → "image data is missing for media {id}"（同旧）
             → 追加 {{GEOF_IMAGE_n}} 并登记 images（MAX_IMAGES 上限不变）
  MARKDOWN → markdown 值原样作为独立块插入（实测值本身即完整 ``` 围栏代码块）
  DIVIDER  → 追加 "---"（独立块，\n\n 连接下为水平线）
  TWEET    → 追加 "> 嵌入推文：[查看原推](https://x.com/i/web/status/{tweet_id})"（保留引用，不丢内容）
  LINK/unknown → 抛 "unsupported entity"（对齐旧 fail-closed 语义；正常文章中 LINK 不会挂在 atomic 上）
```

**Step 7 — 收尾校验（不变）**

- `normalize_inline_text(markdown) < X_ARTICLE_MIN_CHARS(80)` → 抛错
- 返回 `{title, markdown, images, math_count:0, article_marker:"x:article+content_state"}`

### 3.5 SKILL.md 更新

X Article 段落改写为：支持两种序列化（旧 Draft.js 与现行 content_state）；说明 markdown 卡片、分隔线、嵌入推文链接、视频取首帧的处理；明确"块/实体/图片映射不完整即中止"的 fail-closed 语义保持不变。

## 4. fail-closed 语义保持（设计约束清单）

1. 无 `x-article-body` 可见标记 → 中止（不变）
2. 流中既无旧也无新序列化标记 → 中止（错误信息更新）
3. 新格式块序列消费中断（结构异常）→ 中止，绝不跳过坏块
4. atomic 块无实体引用 → 中止（同旧）
5. atomic 块引用无法解析的实体类型 → 中止（同旧）
6. MEDIA 实体缺 media_id / 引用的 media_id 无 URL → 中止（同旧）
7. MARKDOWN 实体值不是合法 JS 字符串 → 中止
8. 标题缺失 → 中止（同旧）
9. 正文 < 80 字符 → 中止（同旧）
10. 不生成任何纯文本降级；文件仅在解析+图片下载全部完成后 create-only 写入（不变）

## 5. 暂不做（记录为后续可选增强，防止本次改动范围膨胀）

| 增强 | 说明 | 评估 |
|---|---|---|
| 内联 LINK 渲染 | 新格式 entity_ranges(offset,length)+LINK 实体可还原文内链接 | 旧解析器同样丢弃内联链接，维持行为一致；offset 按 UTF-16 还是码点计数需用带 emoji+链接的样本先验证，盲做有错位风险 |
| Bold/Italic 内联样式 | `inline_style_ranges:{style:"Bold"}` | 同上，且旧格式从未渲染 |
| 嵌入推文全文渲染 | 流内含被嵌推文的完整序列化数据（作者/正文），可渲染为引用块 | 阶段一先保链接不丢内容；全文渲染另行评估 |
| 视频源保留 | ApiVideo 的 variants 含 mp4 URL | 阶段一取 poster 图（与旧行为等价） |
| published 回退 | 流内 metadata.first_published_at_secs 可补 meta 缺失场景 | 现有 `article:published_time` meta 在新页面仍存在，暂无必要 |

## 6. 部署流程（按现有 GeoF 约定）

1. **备份**：部署目录下的 `<ts>_before_x_content_state_fix/`（沿用现有 `before_*` 命名约定）
2. **修改源**：注册表登记的 skill 源目录（`source_path`，即部署真源）
   - `scripts/capture_and_publish.py`（§3 设计）
   - `SKILL.md`（§3.5）
3. **版本与哈希**：
   - skill 注册表：`version` 2.3.0 → **2.4.0**
   - 用注册表自带的 `directory_sha256()` 重算目录哈希，更新 `sha256` 字段（watcher 每任务加载注册表并校验源目录哈希，不更新必然报 hash mismatch）
4. **同步镜像**：运行时 skill 目录（如 `~/.claude/skills/wechat-to-obsidian/`）与本地 skill 镜像目录（仅同步 SKILL.md + scripts/，测试物料不放 sealed 目录）
5. 无需重启 watcher（注册表按任务加载）

## 7. 测试与验证方案

1. **离线解析测试**（新格式，零网络依赖）
   - 固定 4 个已抓取样本为 fixture（存于 skill 目录之外，如 `tests/fixtures/x_articles/`）
   - 断言：标题/封面/块数/图片数/实体类型集合/正文长度/生成 markdown 关键片段
2. **离线回归测试**（旧格式）
   - 用合成旧格式 fixture（按旧正则结构构造）验证 `extract_x_article_draftjs` 路径不回归
3. **微信/xAI 回归**：取一条近期成功的 mp.weixin 链接与新 x.ai/news 链接离线跑解析，确认零改动路径无恙
4. **Canary 端到端**（不触生产库）：临时目录构造假 vault（00-Inbox/06-Attachments/00-images）+ `.geof_cobs_task.json`，`GEOF_COBS_CANARY=1 python3 scripts/capture_and_publish.py`，断言 `publish_result.json` 产出、note/图片落盘、图片计数与失败计数
5. **生产验证**：先重投 1–2 条归档失败链接，人工核对生产 vault 中笔记内容与图片；通过后重投其余失败链接
6. **监控**：新 `article_marker: "x:article+content_state"` 可在 audit JSON 中直接观测解析路径

## 8. 风险与边界

| 风险 | 缓解 |
|---|---|
| x.com 再次改键序/结构 | 所有解析步骤结构不符即抛错（fail-closed），错误信息明确指出中断位置，不产出坏笔记 |
| 页面含多个 ArticleEntity（嵌入/推荐文章）串扰 | 全部扫描限定在 [content_state, title_end] 主对象区域内；media_id 为全局唯一雪花 ID，即使多登记也无冲突 |
| 含未知实体类型的文章 | fail-closed 中止并报实体类型名（与旧 "unsupported entity" 一致），人工可见、可快速跟进 |
| 大文章（516 块实测） | MAX_X_DRAFT_BLOCKS=1000、MAX_IMAGES=60、图片总量 80MB 上限均不变 |
| fixture 存隐私/体积 | 存放于 sealed skill 目录之外，不参与目录哈希 |
