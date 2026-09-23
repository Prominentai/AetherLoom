"""Explain estimated Anlas billing separately from local dispatch eligibility.

Rules follow NovelAI's public image client and docs, reviewed 2026-09-23.
https://novelai.net/_next/static/chunks/1601-570fd9e21fc1a9a0.js
https://novelai.net/_next/static/chunks/266-9080def4f2212ae0.js
https://docs.novelai.net/en/image/precisereference/
No request flags, account balance changes, image decoding or HTTP calls here.
The server remains authoritative; a positive balance is not a free entitlement.
"""
import copy
import math
import time
from .catalog import default_options, validate_options, director_size

FREE_PIXELS = 1_048_576
ACCOUNT_MAX_AGE = 60


def _number(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except (ValueError, OverflowError):
        return False


def _account(evidence, now):
    if not isinstance(evidence, dict) or not isinstance(evidence.get('data'), dict):
        return None, '尚未查询该任务所用 Token 的账户。'
    stamp = evidence.get('updated_at')
    if not _number(stamp) or not 0 <= now - stamp <= ACCOUNT_MAX_AGE:
        return None, '账户查询已过期，需刷新后确认会员与额度。'
    data = evidence['data']
    tier = data.get('tier')
    if isinstance(tier, bool) or tier not in (0, 1, 2, 3):
        return None, '账户未返回已知会员等级。'
    if tier == 0:
        return None, '未订阅账户可能涉及共享试用资格，不能仅按会员等级判定收费。'
    if tier in (1, 2):
        return False, '当前账户为 Tablet / Scroll，不享受 Opus 免费生成。'
    expiry = data.get('expires_at')
    if not _number(expiry):
        return None, 'Opus 有效期未返回，无法确认免费资格。'
    if expiry <= now:
        if data.get('account_type') == 0:
            return False, '普通账户的 Opus 已到期，不享受免费生成。'
        return None, 'Opus 已过期但账户类型未确认，无法排除特殊免费资格。'
    # active=False can mean cancelled renewal; an unexpired subscription is usable.
    return True, 'Opus 在有效期内。'


def _size(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    if any(not _number(v) or v <= 0 or int(v) != v for v in value):
        return None
    return int(value[0]), int(value[1])


def estimate(snapshot, account_evidence=None, input_size=None, *, now=None):
    """Return public category/reason/extras and an independent parallel decision.

    `input_size` is the prepared input's dimensions for standalone tools. For
    generation (including focused infill) only the requested output dimensions
    matter, not the full source canvas or the final recomposited output.
    """
    now = time.time() if now is None else now
    def result(category, reason, parallel=False, extras=''):
        return dict(category=category, reason=reason, extras=extras,
                    parallel_eligible=bool(parallel))
    if not isinstance(snapshot, dict):
        return result('unknown', '绘图参数格式无效。')
    options = default_options()
    options.update(copy.deepcopy(snapshot))
    action = options.get('action')
    if action in ('upscale', 'augment'):
        options.update(references=[], characters=[], n_samples=1)
        if action == 'upscale':
            options['model'] = 'nai-diffusion-5-curated'
    if action == 'infill' and not options.get('mask_path'):
        # Drawing drafts/alpha inputs are materialized by composition.prepare.
        # This placeholder is solely for parameter validation, never sent/saved.
        options['mask_path'] = '<prepared-mask>'
    try:
        options = validate_options(options)
    except (ValueError, TypeError, KeyError, OverflowError):
        return result('unknown', '参数尚未通过校验，无法确认计费。')
    opus, account_reason = _account(account_evidence, now)
    if action in ('augment', 'upscale'):
        size = _size(input_size)
        if size is None:
            return result('unknown', '等待固化输入图像，按实际图像尺寸确认工具计费；生成宽高和步数不适用。')
        if action == 'upscale':
            if size[0] * size[1] > 3_145_728:
                return result('unknown', '当前官网超分报价只覆盖至 3,145,728 输入像素；此尺寸的接口支持与费用未确认。')
            return result('paid', '当前独立 2× 超分接口按输入面积收费，不沿用旧版小图免费规则；工具暂按串行执行。')
        size = director_size(*size)
        method = options['augment_method']
        if method == 'pixel-snap':
            return result('unknown', '官网 Pixel Snap 在本地计算；当前接口路线的费用未确认，不能套用官网零费用。')
        if method == 'bg-removal':
            return result('paid', '移除背景有独立工具费用，Opus 也不免费；工具暂按串行执行。')
        if size[0] * size[1] > FREE_PIXELS:
            return result('paid', f'Director 处理尺寸 {size[0]} × {size[1]}，超过 1,048,576 像素免费上限；工具暂按串行执行。')
        if opus is False:
            return result('paid', account_reason + '此 Director 工具按次收费，暂按串行执行。')
        if opus is None:
            return result('unknown', account_reason + '该工具在有效 Opus 与规定尺寸内可免费。')
        return result('free', '有效 Opus，处理图像不超过 1,048,576 像素；此 Director 工具适用免费规则，不占 V5 生成额度。')
    if action not in ('generate', 'img2img', 'infill'):
        return result('unknown', '此操作的计费规则尚未确认。')
    if options.get('upscaled_enhance'):
        return result('unknown', 'Max Enhance 由服务端放大并调整最终尺寸；不能按输入图像面积判定免费，费用以实际 Anlas 扣费为准。')
    pixels = options['width'] * options['height']
    refs = [ref for ref in options.get('references', []) if ref.get('enabled', True)]
    precise = [ref for ref in refs if ref.get('kind') != 'vibe']
    vibe = [ref for ref in refs if ref.get('kind') == 'vibe']
    extras = ('Vibe 编码缓存未命中时另收编码费；缓存会受前序任务影响，此处不计入图像生成免费资格。'
              if vibe else '')
    reasons = []
    if options['n_samples'] > 1:
        reasons.append(f"一次请求 {options['n_samples']} 张图，不能全部免费")
    if options['steps'] > 28:
        reasons.append(f"{options['steps']} 步超过 28 步免费上限")
    if pixels > FREE_PIXELS:
        reasons.append(f"请求 {options['width']} × {options['height']}，面积超过 1,048,576 像素免费上限")
    if precise:
        extras = f'Precise Reference 按 {len(precise)} 张参考图、每张结果另收参考费用。'
    elif len(vibe) > 4:
        extras += '超过 4 张 Vibe 的部分另有每次生成附加费。'
    if reasons:
        return result('paid', '；'.join(reasons) + '。', True, extras)
    if opus is False:
        return result('paid', account_reason, True, extras)
    if precise or len(vibe) > 4:
        reason = ('Precise Reference 每次生成均有附加费用。' if precise else 'Vibe 超过 4 张，每次生成均有附加费用。')
        return result('paid', reason + '基础生成仍可能使用 Opus 免费抵扣；附加收费不能证明已绕过免费并发限制，因此串行。', extras=extras)
    if opus is None:
        return result('unknown', account_reason + '当前参数符合免费规格，但需确认账户资格。', extras=extras)
    if options['model'].startswith('nai-diffusion-5-'):
        data = account_evidence['data']
        negative, percent = data.get('negative'), data.get('percent')
        if negative is True:
            return result('paid', '查询时 V5 免费额度已耗尽，预计使用 Anlas；额度持续恢复，为避免执行时转为免费，本任务仍串行。', extras=extras)
        if negative is not False or not _number(percent) or percent <= 0:
            return result('unknown', 'V5 免费额度未确认或处于零值边界，无法保证免 Anlas。', extras=extras)
        reason = '有效 Opus，单图、面积不超过 1,048,576 像素且不超过 28 步；查询时 V5 尚有额度，执行前其他任务可能消耗额度。'
    else:
        reason = '有效 Opus，单图、面积不超过 1,048,576 像素且不超过 28 步；V4.5 不受 V5 额度限制。'
    if vibe:
        return result('unknown', reason + '主图预计免费，整项任务是否收费还取决于 Vibe 编码缓存。', extras=extras)
    return result('free', reason)
