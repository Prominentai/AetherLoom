"""Prompt structures and official quality/UC presets, public web build 2026-09-22."""
from copy import deepcopy
import secrets
import re
from .catalog import active_characters

_HEAVY = "lowres, artistic error, film grain, scan artifacts, worst quality, bad quality, jpeg artifacts, very displeasing, chromatic aberration, dithering, halftone, screentone, multiple views, logo, too many watermarks, negative space, blank page"
_CURATED = "blurry, lowres, upscaled, artistic error, film grain, scan artifacts, worst quality, bad quality, jpeg artifacts, very displeasing, chromatic aberration, halftone, multiple views, logo, too many watermarks, negative space, blank page"
_FURRY = "{worst quality}, distracting watermark, unfinished, bad quality, {widescreen}, upscale, {sequence}, {{grandfathered content}}, blurred foreground, chromatic aberration, sketch, everyone, [sketch background], simple, [flat colors], ych (character), outline, multiple scenes, [[horror (theme)]], comic"


def _join(*parts):
    return ", ".join(p.strip().strip(",").strip() for p in parts if p and p.strip().strip(",").strip())


_TEXT_MARKER = re.compile(r"(?:^|\s|[,.:[\]{}、。])text:(?!:)", re.IGNORECASE)


def _append_before_text(prompt, suffix):
    """Quality/enhance tags must not become text that the image should render."""
    marker = _TEXT_MARKER.search(prompt)
    if not suffix or marker is None:
        return _join(prompt, suffix)
    before = _join(prompt[:marker.start()], suffix)
    delimiter = marker.group()
    if delimiter.lower() == "text:":
        delimiter = ", " + delimiter
    return before + delimiter + prompt[marker.end():]


def _dataset_prompt(prompt, mode):
    """Match the public web mode prefix; explicit dataset prefixes take priority.

    Anime has no additional tag. Furry changes only the base positive caption,
    leaving character/negative captions and the user's saved text untouched.
    https://docs.novelai.net/en/image/tags/#dataset-tags
    """
    if mode != "furry" or prompt.startswith(("fur dataset", "background dataset")):
        return prompt
    return "fur dataset, " + prompt


def quality_text(model, preset):
    if preset == "none":
        return ""
    if preset == "light":
        return "very aesthetic, amazing quality, no text"
    if model == "nai-diffusion-4-5-curated":
        return "very aesthetic, masterpiece, no text, -0.8::feet::, rating:general"
    return "very aesthetic, masterpiece, no text"


def negative_text(model, preset):
    if preset == "none":
        return ""
    if preset == "furryFocus":
        return _FURRY
    if model == "nai-diffusion-4-5-curated":
        if preset == "heavy":
            return _CURATED
        if preset == "light":
            return "blurry, lowres, upscaled, artistic error, scan artifacts, jpeg artifacts, logo, too many watermarks, negative space, blank page"
        return "blurry, lowres, upscaled, artistic error, film grain, scan artifacts, bad anatomy, bad hands, worst quality, bad quality, jpeg artifacts, very displeasing, chromatic aberration, halftone, multiple views, logo, too many watermarks, @_@, mismatched pupils, glowing eyes, negative space, blank page"
    if preset == "heavy":
        return _HEAVY
    if preset == "humanFocus":
        return _HEAVY + ", @_@, mismatched pupils, glowing eyes, bad anatomy"
    if model.startswith("nai-diffusion-5-"):
        return "lowres, bad hands, bad anatomy, artistic error, sepia, white haze, worst quality, very displeasing, jpeg artifacts, 0::ai-generated::"
    return "lowres, artistic error, scan artifacts, worst quality, bad quality, jpeg artifacts, multiple views, very displeasing, too many watermarks, negative space, blank page"


def build_prompts(options):
    """Return API prompt fields without mutating the user's text or snapshot."""
    model = options["model"]
    suffix = quality_text(model, options["quality_preset"])
    # The web app applies transparency at the start of the quality suffix.
    if options.get("transparent_background") and not any(
            tag.strip() == "transparent background" for tag in options["prompt"].split(",")):
        suffix = _join("transparent background", suffix)
    positive = _append_before_text(options["prompt"], suffix)
    if (options.get("enhancement") and not options.get("upscaled_enhance")
            and "upscaled, blurry" not in positive):
        positive = _append_before_text(positive, "-2::upscaled, blurry::")
    positive = _dataset_prompt(positive, options.get("dataset_mode", "anime"))
    preset = negative_text(model, options["uc_preset"])
    negative = _join(preset, options["negative_prompt"])
    # Full models prepend this UC guard only while a UC preset is selected and
    # the actual positive prompt has not explicitly requested that tag.
    if model.endswith("-full") and preset and "nsfw" not in options["prompt"].lower():
        negative = _join("nsfw", negative)
    characters = active_characters(options)
    use_coords = bool(characters and characters[0]["use_coords"])
    pos_chars, neg_chars = [], []
    for ch in characters:
        center = [{"x": ch["x"], "y": ch["y"]}]
        pos_chars.append({"char_caption": ch["prompt"], "centers": center})
        neg_chars.append({"char_caption": ch["negative_prompt"], "centers": center})
    return dict(prompt=positive, negative_prompt=negative,
                v4_prompt=dict(caption=dict(base_caption=positive, char_captions=pos_chars),
                               use_coords=use_coords, use_order=True),
                v4_negative_prompt=dict(caption=dict(base_caption=negative, char_captions=neg_chars),
                                        legacy_uc=False))


def resolve_options(options, seed=None):
    """Resolve chunks and each ||a|b|| section independently of the image seed.

    The seed argument is retained for call compatibility only. The official
    randomizer draws again on every generation, including fixed-seed requests:
    https://docs.novelai.net/en/image/promptrandomizer/
    """
    result = deepcopy(options)
    chunks = options.get("chunks", {})
    if not isinstance(chunks, dict) or len(chunks) > 500 or any(
            not isinstance(k, str) or not isinstance(v, str) or len(v) > 30000
            for k, v in chunks.items()):
        raise ValueError("Prompt Chunks 必须是名称到文本的字典，每项不超过 30000 字符")
    macro = re.compile(r"!macro:([^!]+)!")
    expansions = 0

    def expand_chunks(text, stack=()):
        nonlocal expansions
        parts, position, length = [], 0, 0
        for match in macro.finditer(text):
            expansions += 1
            if expansions > 2048:
                raise ValueError("Prompt Chunks 展开次数过多，请简化引用")
            name = match[1]
            if name not in chunks:
                raise ValueError("找不到 Prompt Chunk：" + name[:80])
            if name in stack or len(stack) >= 16:
                raise ValueError("Prompt Chunks 包含循环引用或嵌套过深")
            replacement = expand_chunks(chunks[name], stack + (name,))
            literal = text[position:match.start()]
            length += len(literal) + len(replacement)
            if length > 100000:
                raise ValueError("展开后的提示词过长")
            parts.extend((literal, replacement))
            position = match.end()
        length += len(text) - position
        if length > 100000:
            raise ValueError("展开后的提示词过长")
        parts.append(text[position:])
        return "".join(parts)

    def expand(text):
        text = expand_chunks(text)
        # The public frontend also splits on double bars first, keeping single
        # bars outside a section available for multi-character prompt syntax.
        sections = text.split("||")
        if len(sections) % 2 == 0:
            raise ValueError("随机提示词的双竖线未配对，应为 ||选项一|选项二||")
        for index in range(1, len(sections), 2):
            sections[index] = secrets.choice(sections[index].split("|"))
        return "".join(sections)

    for key in ("prompt", "negative_prompt"):
        result[key] = expand(result[key])
    for ch in active_characters(result):
        for key in ("prompt", "negative_prompt"):
            ch[key] = expand(ch[key])
    return result
