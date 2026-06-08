"""Headless prompt builder for the Claude Code agent.

The Step 4 "Eight Confirmations" are now resolved from per-request options with
smart defaults: a field that the caller leaves empty falls back to the default
text below (so "just drop a file" still works), while a provided value overrides
that single confirmation. Nothing here blocks — every value is baked into the
prompt and the agent runs non-interactively.

This prompt also neutralizes the two automation blockers in SKILL.md:
  - Step 4 is ⛔ BLOCKING -> all eight are pre-answered with auto-continue.
  - Step 6 auto-starts a live-preview server -> we forbid starting it.
"""

from __future__ import annotations

from pathlib import Path

# ── Smart defaults for each confirmation (used when the caller omits a field) ──
DEFAULTS = {
    "page_count": "依据素材体量自动决定，建议 12–18 页，不要硬凑也不要过度精简",
    "audience": "通用商务 / 技术专业读者",
    "style_objective": "专业、现代、信息层级清晰，适合正式汇报场景",
    "color_scheme": "由策划根据素材主题自定一套协调、专业的配色（深浅对比清晰，主色贴合主题）",
    "typography": "无衬线字体，中英文混排",
}

_ICON_TEXT = {
    "line": "使用内置图标库（线性风格图标），按内容语义选用",
    "filled": "使用内置图标库（实心风格图标），按内容语义选用",
    "none": "不使用图标",
}

_FORMULA_TEXT = {
    "mixed": "公式渲染策略 mixed（复杂公式渲染为 PNG，简单表达式保留可编辑文本）",
    "render-all": "公式渲染策略 render-all（所有公式都渲染为 PNG）",
    "text-only": "公式渲染策略 text-only（不渲染公式，全部保留为可编辑文本/Unicode）",
}

_IMAGE_TEXT = {
    "ai": "关键页使用 AI 生成配图（Acquire Via: ai，模型 gpt-image-2），其余使用占位或免配图，确保全流程无需人工补图",
    "web": "关键页使用网络图片搜索（Acquire Via: web），其余使用占位，确保全流程无需人工补图",
    "placeholder": "全部使用占位图（Acquire Via: placeholder），不实际拉取图片",
    "none": "不使用任何配图，纯文字/图形版式",
}


def _opt(options: dict, key: str) -> str | None:
    value = options.get(key)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


# ── Style presets ─────────────────────────────────────────────────────────────
# Each preset resolves to the underlying eight-confirmation fields the prompt
# already consumes: a style_objective that steers the Executor to the right
# reference file (executor-general / executor-consultant / executor-consultant-top),
# a color scheme (config.py DESIGN_COLORS), default typography and icon style.
STYLE_PRESETS: dict[str, dict[str, str]] = {
    "modern": {
        "style_objective": ("通用现代风格（对应 executor-general）：信息层级清晰、留白克制、"
                            "适合大多数商务/技术汇报。"),
        "color_scheme": "通用现代配色（general）：蓝绿橙点缀、白底、专业明快。",
        "typography": "无衬线字体，中英文混排。",
        "icon_style": "line",
    },
    "mckinsey": {
        "style_objective": ("顶级咨询/麦肯锡(MBB)风格（对应 executor-consultant-top）：金字塔结构、"
                            "结论先行、每页一句 Takeaway、数据必配对比基准、SCQA 叙事。"),
        "color_scheme": "咨询配色（consulting）：深蓝主色 + 克制点缀，庄重权威。",
        "typography": "无衬线字体，信息密度偏高（body 14–18px 基线）。",
        "icon_style": "line",
    },
    "consulting": {
        "style_objective": ("商务咨询风格（对应 executor-consultant）：论点清晰、图表驱动、"
                            "专业稳重，适合方案/尽调/汇报。"),
        "color_scheme": "咨询配色（consulting）：深蓝主色，专业沉稳。",
        "typography": "无衬线字体，中英文混排。",
        "icon_style": "line",
    },
    "tech": {
        "style_objective": ("科技/产品风格（对应 executor-general）：现代、利落、强调架构与流程，"
                            "适合产品发布、技术方案。"),
        "color_scheme": "科技配色（tech）：深色背景 + 青紫荧光点缀，未来感。",
        "typography": "无衬线字体，中英文混排。",
        "icon_style": "line",
    },
    "academic": {
        "style_objective": ("学术/科研风格（对应 executor-general）：严谨克制、重公式与图表、"
                            "适合论文汇报、开题答辩、学术报告。"),
        "color_scheme": "学术配色（academic）：暗红 + 深蓝 + 金，沉稳学术。",
        "typography": "无衬线字体为主，公式清晰；中英文混排。",
        "icon_style": "line",
    },
    "government": {
        "style_objective": ("政务/党政风格（对应 executor-general）：庄重、权威、规范，"
                            "适合工作汇报、政策解读、单位总结。"),
        "color_scheme": "政务配色（government）：党政红 + 深蓝 + 金，庄重正式。",
        "typography": "无衬线字体，标题厚重；中英文混排。",
        "icon_style": "filled",
    },
}


def resolve_style_preset(preset: str | None) -> dict[str, str]:
    """Translate a style preset key into the underlying option fields. Unknown /
    empty falls back to 'modern'."""
    key = (preset or "modern").strip().lower()
    return STYLE_PRESETS.get(key, STYLE_PRESETS["modern"])


def resolve_eight(canvas_format: str, options: dict) -> dict[int, str]:
    """Resolve the eight confirmations from options + defaults."""
    icon_style = (_opt(options, "icon_style") or "line").lower()
    formula_policy = (_opt(options, "formula_policy") or "mixed").lower()
    image_mode = (_opt(options, "image_mode") or "ai").lower()
    typography = _opt(options, "typography") or DEFAULTS["typography"]

    return {
        1: f"画布格式：{canvas_format}。",
        2: f"页数范围：{_opt(options, 'page_count') or DEFAULTS['page_count']}。",
        3: f"目标受众：{_opt(options, 'audience') or DEFAULTS['audience']}。",
        4: f"风格目标：{_opt(options, 'style_objective') or DEFAULTS['style_objective']}。",
        5: f"配色方案：{_opt(options, 'color_scheme') or DEFAULTS['color_scheme']}。",
        6: f"图标使用：{_ICON_TEXT.get(icon_style, _ICON_TEXT['line'])}。",
        7: f"字体与排版：{typography}；{_FORMULA_TEXT.get(formula_policy, _FORMULA_TEXT['mixed'])}。",
        8: f"配图方式：{_IMAGE_TEXT.get(image_mode, _IMAGE_TEXT['ai'])}。",
    }


def _image_directive(options: dict) -> str:
    image_mode = (_opt(options, "image_mode") or "ai").lower()
    if image_mode == "ai":
        return ("配图走 AI：对 Acquire Via: ai 的图片行，使用 image_gen.py 的 manifest 模式调用 "
                "gpt-image-2（环境已配置好 OPENAI_* 中转）。生成失败的行重试一次后标记 "
                "Needs-Manual 并继续，不要因为缺图而停下。")
    if image_mode == "web":
        return ("配图走网络搜索：对 Acquire Via: web 的图片行，使用 image_search.py。"
                "失败的行标记 Needs-Manual 并继续，不要因为缺图而停下。")
    return "不进行任何在线配图：所有图片行使用占位或免配图，禁止调用图片生成/搜索脚本。"


# Languages the API exposes explicitly; anything else is passed through verbatim
# as a free-text language name. "auto" (or empty) keeps the source language.
_LANGUAGE_NAMES = {
    "auto": None,
    "zh": "简体中文",
    "zh-cn": "简体中文",
    "zh-hans": "简体中文",
    "zh-tw": "繁体中文",
    "zh-hant": "繁体中文",
    "en": "English",
    "ja": "日本語",
    "ko": "한국어",
    "fr": "Français",
    "de": "Deutsch",
    "es": "Español",
    "ru": "Русский",
}


def resolve_language(options: dict | None) -> str | None:
    """Return the human-readable target language name, or None to follow source."""
    raw = _opt(options or {}, "output_language")
    if not raw:
        return None
    key = raw.strip().lower()
    if key in _LANGUAGE_NAMES:
        return _LANGUAGE_NAMES[key]
    # Unknown code/name: pass through as-is so callers can request any language.
    return raw.strip()


def _language_directive(options: dict) -> str:
    """The '# 语言' prompt block. Default follows the source; a specified target
    language forces BOTH the slide text and the speaker notes into that language,
    while still reading the (possibly different-language) source for facts."""
    target = resolve_language(options)
    if target is None:
        return "- 响应语言与源素材保持一致。"
    return (
        f"- 目标输出语言：{target}。\n"
        f"- 阅读源素材时按其原文理解（源素材可能是其他语言，例如英文），"
        f"但**幻灯片正文/标题/图表文字与演讲者备注（notes/total.md）全部统一用 {target} 输出**。\n"
        f"- 需要时把源素材内容准确翻译为 {target}；专有名词/缩写/公式可保留原文。\n"
        f"- 同一份 deck 内不得混用语言：除上述保留项外，一律使用 {target}。"
    )


def build_generation_prompt(project_path: Path, source_basename: str,
                            canvas_format: str, options: dict | None = None) -> str:
    """Compose the full instruction sent to `claude -p`."""
    options = options or {}
    eight = "\n".join(f"  {n}. {text}" for n, text in resolve_eight(canvas_format, options).items())
    image_directive = _image_directive(options)
    language_directive = _language_directive(options)

    return f"""\
你现在以无人值守（headless）方式运行 ppt-master 工作流，为一个 HTTP 服务自动生成 PPTX。
请严格按 SKILL.md 的串行流水线执行，从 Step 4 一直跑到 Step 7 导出 PPTX。

# 项目信息
- 项目目录（已由服务创建并完成 Step 1–3）：{project_path}
- 源素材已通过 import-sources 放入：{project_path}/sources/（原始文件名包含「{source_basename}」，并已转好 Markdown）
- 你不需要重新 init 项目或重新 import；直接从 Step 4（Strategist）开始。

# Step 4 八项确认（已代用户确认，禁止再向用户提问、禁止阻塞等待）
{eight}

# 无人值守硬性约束（违反即失败）
1. 全程非交互：不得停下来等待用户输入。八项确认视为「已确认」，直接产出 design_spec.md / spec_lock.md 后继续。
2. 禁止启动实时预览：绝对不要运行 svg_editor/server.py，不要尝试打开浏览器或起任何常驻服务进程（会卡死服务）。
3. {image_directive}
4. SVG 必须由你本人逐页手写（遵守 SKILL.md 全局纪律第 6/7/9 条），不得写脚本批量生成 SVG。
5. 质检：所有 SVG 生成后运行 svg_quality_checker.py，修掉所有 error 再进入后处理。
6. 后处理与导出按 Step 7 顺序逐条执行：total_md_split.py → finalize_svg.py → svg_to_pptx.py。
7. 最终必须在 {project_path}/exports/ 下产出一个 .pptx 文件。完成后，用一行输出明确打印：
   PPTX_READY: <exports 下的 .pptx 绝对路径>

# 语言
{language_directive}

现在开始执行 Step 4。"""


def build_resume_prompt(project_path: Path, source_basename: str,
                        canvas_format: str, options: dict | None = None) -> str:
    """Compose a resume instruction: continue from on-disk artifacts instead of
    redoing completed work. Used for automatic retries and the /retry endpoint.

    Resumption is at *artifact granularity*: the agent inspects which pipeline
    outputs already exist and continues from the first incomplete step. This
    preserves the most expensive work (per-page SVGs already in svg_output/)."""
    options = options or {}
    eight = "\n".join(f"  {n}. {text}" for n, text in resolve_eight(canvas_format, options).items())
    image_directive = _image_directive(options)
    language_directive = _language_directive(options)

    return f"""\
你正在以无人值守（headless）方式【续跑】一个**之前被中断**的 ppt-master 任务。
项目目录里可能已经存在上一次运行产出的部分中间产物，请**从第一个未完成的步骤继续，已完成的不要重做**。

# 项目目录（已由服务创建并完成 Step 1–3，禁止重新 init / import）
{project_path}
- 源素材在 {project_path}/sources/（原始文件名含「{source_basename}」，已转 Markdown）

# 先做断点判定（按此顺序检查产物是否存在，决定从哪一步继续）
1. {project_path}/spec_lock.md 与 design_spec.md —— 若**都存在**，说明 Phase A（Step 4/5）已完成，**跳过策划**，直接进入执行阶段；若缺失，从 Step 4 开始补齐。
2. {project_path}/images/ —— 若 spec_lock 引用的图片已存在则不要重复生成；仅补齐缺失项。
3. {project_path}/svg_output/*.svg —— 已生成的页**保留不动**，只生成 spec_lock 中尚未产出的页（按文件名比对缺哪页补哪页）。
4. {project_path}/notes/total.md —— 缺失才补。
5. {project_path}/svg_final/ 与 {project_path}/exports/*.pptx —— 若 svg_output 已齐但尚未导出，直接执行 Step 7 后处理与导出。

# Step 4 八项确认（已代用户确认，禁止再向用户提问、禁止阻塞）
{eight}

# 无人值守硬性约束（违反即失败）
1. 全程非交互，不得停下等待用户输入。
2. 禁止启动实时预览（绝不运行 svg_editor/server.py 或任何常驻进程）。
3. {image_directive}
4. SVG 必须由你本人逐页手写，不得写脚本批量生成；只补缺页，不重画已存在的页。
5. 质检：所有 SVG 就绪后运行 svg_quality_checker.py，修掉所有 error 再进入后处理。
6. 后处理与导出按 Step 7 顺序执行：total_md_split.py → finalize_svg.py → svg_to_pptx.py。
7. 最终必须在 {project_path}/exports/ 下产出一个 .pptx，并用一行明确打印：
   PPTX_READY: <exports 下的 .pptx 绝对路径>

# 语言
{language_directive}

现在先做断点判定，然后从第一个未完成的步骤继续执行。"""
