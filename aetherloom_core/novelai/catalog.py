"""Verified public NovelAI image options; no network or Qt dependencies.

Protocol checked against image.novelai.net/docs/doc.json and the public
novelai.net/image application (36c939a-production), 2026-09-23. V5 Curated's
web inpaint action switches to V4.5; this client deliberately does not do that.
"""
from copy import deepcopy
import math

SAMPLERS = ("k_euler_ancestral", "k_euler", "k_dpmpp_2s_ancestral",
            "k_dpmpp_2m", "k_dpmpp_2m_sde", "k_dpmpp_sde", "k_dpm_2")
NOISE_SCHEDULES = ("karras", "exponential", "polyexponential")
DATASET_MODES = ("anime", "furry")
AUGMENT_METHODS = ("bg-removal", "lineart", "sketch", "colorize", "emotion",
                   "declutter", "declutter-keep-bubbles")
MAX_GENERATION_PIXELS = 3_145_728
MAX_STORED_CHARACTERS = 256


def _caps(model):
    v5 = model.startswith("nai-diffusion-5-")
    curated = model.endswith("curated")
    return dict(img2img=True, infill=not (v5 and curated),
                inpaint_model=None if v5 and curated else model + "-inpainting",
                vibe=not v5, precise=not v5, characters=True,
                max_characters=32 if v5 else 6, free_coordinates=v5,
                transparency=v5, stream=True, noise_schedule=not v5,
                variety_boost=not v5, upscale=True, augment=True,
                enhance_prompt_add=True, max_enhance=v5,
                quality_presets=["standard", "light", "none"] if v5 else ["standard", "none"],
                uc_presets=["heavy", "light", "humanFocus", "none"] if curated and not v5
                else ["heavy", "light", "furryFocus", "humanFocus", "none"])


MODELS = [dict(id=i, name=n, capabilities=_caps(i)) for i, n in (
    ("nai-diffusion-5-full", "NAI Diffusion V5 Full"),
    ("nai-diffusion-5-curated", "NAI Diffusion V5 Curated"),
    ("nai-diffusion-4-5-full", "NAI Diffusion V4.5 Full"),
    ("nai-diffusion-4-5-curated", "NAI Diffusion V4.5 Curated"))]


def capabilities(model):
    for item in MODELS:
        if item["id"] == model:
            return deepcopy(item["capabilities"])
    raise ValueError("不支持的 NovelAI 模型，请选择列表中的模型")


def default_options():
    return dict(model=MODELS[0]["id"], action="generate", dataset_mode="anime", prompt="", negative_prompt="",
                width=832, height=1216, steps=23, scale=7.0, sampler="k_euler_ancestral",
                noise_schedule="karras", seed=-1, n_samples=1, cfg_rescale=0.0,
                strength=0.7, noise=0.0, quality_preset="standard", uc_preset="heavy",
                image_path="", mask_path="", characters=[], references=[], reference_mode="vibe", stream=False, timeout=90,
                transparent_background=False, straight_alpha=True, variety_boost=False,
                color_correct=False, add_original_image=True, inpaint_strength=1.0,
                normalize_reference_strength_multiple=True,
                augment_method="colorize", tool_prompt="", defry=0, emotion="neutral", declared_blur_sigma=0.0,
                enhancement=False, upscaled_enhance=False)




def director_size(width, height):
    """Match the web Director's aspect-preserving preprocessing, before billing."""
    width, height = int(width), int(height)
    if width <= 0 or height <= 0:
        raise ValueError("Director 输入图像尺寸无效")
    pixels = width * height
    maximum = MAX_GENERATION_PIXELS - 2000
    if pixels > maximum:
        ratio = math.sqrt(maximum / pixels)
        width, height = max(1, math.floor(width * ratio)), max(1, math.floor(height * ratio))
    pixels = width * height
    if pixels < 1_011_712:
        ratio = math.sqrt(1_048_576 / pixels)
        width, height = max(1, math.floor(width * ratio)), max(1, math.floor(height * ratio))
    return width, height


def reference_defaults(model, kind="vibe"):
    """Defaults for new cards, without changing existing or imported references."""
    return dict(kind=kind, enabled=True, strength=0.6 if kind == "vibe" else 1.0,
                fidelity=1.0, information_extracted=0.7 if kind == "vibe"
                and model == "nai-diffusion-4-5-full" else 1.0)


def active_references(options):
    return [ref for ref in options.get("references", [])
            if isinstance(ref, dict) and ref.get("enabled", True)]


def active_characters(options):
    """Retain card order while omitting disabled local drafts from requests."""
    return [character for character in options.get("characters", [])
            if isinstance(character, dict) and character.get("enabled", True)]


def _finite(value, name):
    try:
        if isinstance(value, bool):
            raise ValueError()
        value = float(value)
        if not math.isfinite(value):
            raise ValueError()
        return value
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name}必须是有限数值") from None


def _number(value, name, lo, hi, integer=False):
    try:
        if isinstance(value, bool):
            raise ValueError()
        v = float(value)
        if not math.isfinite(v) or not lo <= v <= hi or (integer and not v.is_integer()):
            raise ValueError()
        return int(v) if integer else v
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name}必须是 {lo} 到 {hi} 之间的{'整数' if integer else '数值'}") from None


def _text(value, name, limit=30000):
    if not isinstance(value, str) or len(value) > limit or "\0" in value:
        raise ValueError(f"{name}必须是有效文本且不超过 {limit} 个字符")
    return value


def validate_options(options):
    if not isinstance(options, dict):
        raise ValueError("绘图设置必须是字典")
    o = default_options()
    o.update(deepcopy(options))
    if o["action"] not in ("generate", "img2img", "infill", "augment", "upscale"):
        raise ValueError("不支持的绘图操作")
    generation = o["action"] in ("generate", "img2img", "infill")
    o["timeout"] = _number(o["timeout"], "超时时间", 1, 600, True)
    if o["action"] != "generate":
        o["image_path"] = _text(o["image_path"], "图像路径", 32768)
        if not o["image_path"]:
            raise ValueError("请先选择输入图像")
    if not generation:
        # Tools have independent request shapes. Inactive settings remain in the
        # immutable snapshot, but are neither validated nor sent to that API.
        if o["action"] == "upscale":
            o["declared_blur_sigma"] = _number(o["declared_blur_sigma"], "超分辨率模糊系数", 0, 0.5)
            if o["declared_blur_sigma"] not in (0, 0.30, 0.35, 0.40, 0.45, 0.50):
                raise ValueError("超分辨率模糊系数需为 0、0.30、0.35、0.40、0.45 或 0.50")
        else:
            if o["augment_method"] == "pixel-snap":
                raise ValueError("Pixel Snap 是官网本地工具，当前未接入；不能作为 Director API 任务提交")
            if o["augment_method"] not in AUGMENT_METHODS:
                raise ValueError("不支持的 Director Tool")
            if o["augment_method"] in ("colorize", "emotion"):
                if "tool_prompt" not in options:
                    o["tool_prompt"] = o["prompt"]
                o["tool_prompt"] = _text(o["tool_prompt"], "Director 提示词")
                o["defry"] = _number(o["defry"], "Director 强度", 0, 5, True)
            if o["augment_method"] == "emotion":
                o["emotion"] = _text(o["emotion"], "表情", 128)
        return o
    c = capabilities(o["model"])
    if o["dataset_mode"] not in DATASET_MODES:
        raise ValueError("数据集模式必须是 Anime 或 Furry")
    for key, name in (("prompt", "提示词"), ("negative_prompt", "负面提示词")):
        o[key] = _text(o[key], name)
    if o["action"] == "infill":
        o["mask_path"] = _text(o["mask_path"], "蒙版路径", 32768)
    for key in ("enhancement", "upscaled_enhance"):
        if not isinstance(o[key], bool):
            raise ValueError(f"{key} 必须是布尔值")
    if (o["enhancement"] or o["upscaled_enhance"]) and o["action"] != "img2img":
        raise ValueError("增强仅适用于图生图操作")
    if o["upscaled_enhance"] and not c["max_enhance"]:
        raise ValueError("Max Enhance 仅支持 V5 模型")
    dimension_step = 1 if o["upscaled_enhance"] else 32 if o["enhancement"] else 64
    for key in ("width", "height"):
        o[key] = _number(o[key], "宽高", 64, 4096, True)
        if o[key] % dimension_step:
            raise ValueError(f"生成宽高必须是 {dimension_step} 的倍数")
    if o["width"] * o["height"] > MAX_GENERATION_PIXELS:
        raise ValueError("单张生成面积不能超过 3145728 像素")
    if o["upscaled_enhance"] and o["width"] * o["height"] >= 0.8 * MAX_GENERATION_PIXELS:
        raise ValueError("Max Enhance 输入面积必须小于 2516582.4 像素")
    o["scale"] = _finite(o["scale"], "提示词引导")
    if o["scale"] < 0:
        raise ValueError("提示词引导不能为负值")
    for key, name, lo, hi, integer in (
        ("steps", "步数", 1, 50, True),
        ("seed", "种子", -1, 4294967295, True), ("n_samples", "出图数量", 1, 8, True),
        ("cfg_rescale", "CFG Rescale", 0, 1, False)):
        o[key] = _number(o[key], name, lo, hi, integer)
    if o["action"] == "img2img":
        o["strength"] = _number(o["strength"], "重绘强度", 0, 1)
        o["noise"] = _number(o["noise"], "噪声", 0, 1)
    if o["action"] == "infill":
        o["inpaint_strength"] = _number(o["inpaint_strength"], "局部重绘强度", 0, 1)
    boolean_fields = ["stream", "transparent_background", "straight_alpha", "variety_boost",
                      "normalize_reference_strength_multiple"]
    if o["action"] == "infill":
        boolean_fields.append("add_original_image")
    for key in boolean_fields:
        if not isinstance(o[key], bool):
            raise ValueError(f"{key} 必须是布尔值")
    if o["sampler"] not in SAMPLERS:
        raise ValueError("不支持的采样器")
    if o["noise_schedule"] not in NOISE_SCHEDULES:
        raise ValueError("不支持的噪声调度")
    if generation and not c["noise_schedule"] and o["noise_schedule"] != "karras":
        raise ValueError("V5 的噪声调度固定为 karras")
    if generation and o["sampler"] == "k_dpm_2" and o["noise_schedule"] == "karras":
        raise ValueError("DPM2 采样器需要 exponential 或 polyexponential 调度")
    if o["quality_preset"] not in c["quality_presets"] or o["uc_preset"] not in c["uc_presets"]:
        raise ValueError("当前模型不支持选定的质量或负面预设")
    if generation and o["transparent_background"] and not c["transparency"]:
        raise ValueError("透明背景仅支持 V5 模型")
    if generation and o["variety_boost"] and not c["variety_boost"]:
        raise ValueError("当前模型不支持 Variety Boost")
    if o["action"] == "infill":
        if not c["infill"]:
            raise ValueError("V5 Curated 的局部重绘未独立开放；请显式选择 V4.5 Curated 或 V5 Full")
        if not o["mask_path"]:
            raise ValueError("局部重绘需要白色编辑、黑色保留的蒙版")
    if not isinstance(o["characters"], list) or len(o["characters"]) > MAX_STORED_CHARACTERS:
        raise ValueError(f"最多保存 {MAX_STORED_CHARACTERS} 个角色卡片")
    for ch in o["characters"]:
        if not isinstance(ch, dict):
            raise ValueError("角色设置无效")
        ch.setdefault("enabled", True)
        if not isinstance(ch["enabled"], bool):
            raise ValueError("角色启用状态无效")
        if not ch["enabled"]:
            # Inactive cards are editable local drafts: unfinished text, macros,
            # and coordinates do not constrain the current model's request.
            continue
        ch["prompt"] = _text(ch.get("prompt", ""), "角色提示词")
        ch["negative_prompt"] = _text(ch.get("negative_prompt", ""), "角色负面提示词")
        if not ch["prompt"].strip():
            raise ValueError("角色提示词不能为空")
        ch.setdefault("use_coords", False)
        if not isinstance(ch["use_coords"], bool):
            raise ValueError("角色定位开关无效")
        for k in ("x", "y"):
            ch[k] = _number(ch.get(k, 0.5), "角色坐标", 0, 1)
            if ch["use_coords"] and not c["free_coordinates"] and not any(
                    abs(ch[k] - grid) < 1e-6 for grid in (0.1, 0.3, 0.5, 0.7, 0.9)):
                raise ValueError("V4.5 角色坐标需为 0.1、0.3、0.5、0.7 或 0.9")
    enabled_characters = active_characters(o)
    if len(enabled_characters) > c["max_characters"]:
        raise ValueError(f"当前模型最多支持 {c['max_characters']} 个启用角色")
    if len({ch["use_coords"] for ch in enabled_characters}) > 1:
        raise ValueError("角色定位为全局开关，请统一所有角色的定位设置")
    if not isinstance(o["references"], list) or len(o["references"]) > 16:
        raise ValueError("参考图最多 16 张")
    kinds = set()
    for ref in o["references"]:
        if not isinstance(ref, dict):
            raise ValueError("参考图类型无效")
        if not isinstance(ref.get("enabled", True), bool):
            raise ValueError("参考图启用状态无效")
        if not ref.get("enabled", True):
            continue
        if ref.get("kind") not in ("vibe", "character", "style", "character&style"):
            raise ValueError("参考图类型无效")
        kind = "vibe" if ref["kind"] == "vibe" else "precise"
        kinds.add(kind)
        if generation and not c[kind]:
            raise ValueError("Vibe 和 Precise Reference 仅支持 V4.5，请显式切换模型")
        ref["path"] = _text(ref.get("path", ""), "参考图路径", 32768)
        if not ref["path"]:
            raise ValueError("参考图路径不能为空")
        defaults = reference_defaults(o["model"], ref["kind"])
        for key in ("strength", "fidelity", "information_extracted"):
            value = ref.get(key, defaults[key])
            # Official strength/fidelity sliders also allow uncapped, signed
            # numeric entry. Never silently clamp an imported request.
            ref[key] = (_number(value, "信息提取", 0, 1) if key == "information_extracted"
                        else _finite(value, "参考图参数"))
    if len(kinds) > 1:
        raise ValueError("Precise Reference 与 Vibe Transfer 不能同时使用")
    if o["action"] == "infill" and "vibe" in kinds:
        raise ValueError("Vibe Transfer 不支持局部重绘")
    return o


def queue_billing(options):
    """Compatibility dispatch helper; billing.estimate owns all billing rules."""
    from .billing import estimate
    return 'paid' if estimate(options)['parallel_eligible'] else 'serial'
