"""Synchronous, time-bounded translation for Qt workers and the command line.

Engine imports and requests run in a disposable spawned process, isolating
translators' import-time network access and Googletrans's asyncio loop.
"""
import argparse
import asyncio
import importlib.util
import logging
import math
import multiprocessing
from pathlib import Path
import queue
import re
import sys
import time

# Direct execution otherwise imports this file as the third-party engine.
if __package__ in (None, ''):
    script_dir = Path(__file__).resolve().parent
    sys.path[:] = [str(script_dir.parent)] + [
        p for p in sys.path if Path(p or '.').resolve() != script_dir
    ]

LOGGER = logging.getLogger(__name__)
DEPENDENCY_HINT = (
    '自动翻译依赖不可用，请使用客户端的 Python 执行 '
    'python -m pip install -r requirements.txt；也可在 API 管理中配置翻译服务。'
)


def _available(engine):
    try:
        spec = importlib.util.find_spec(engine)
        return spec is not None and (
            not spec.origin or Path(spec.origin).resolve() != Path(__file__).resolve()
        )
    except (ImportError, ValueError, OSError):
        return False


def _arguments(text, target_lang, timeout):
    if not isinstance(text, str):
        raise ValueError('待翻译内容必须是文本')
    language = str(target_lang or '').strip().lower().replace('_', '-')
    if not language:
        raise ValueError('目标语言不能为空')
    timeout = float(timeout)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('翻译超时必须是有限的正数')
    return language, timeout


async def _google_translate(text, language, timeout):
    from googletrans import Translator
    import httpx

    language = 'zh-cn' if language == 'zh' else language
    # Errors otherwise become dummy original-text "translations". Use the
    # library's default API host instead of obsolete .cn/token endpoints.
    async with Translator(timeout=httpx.Timeout(timeout), raise_exception=True) as client:
        result = await asyncio.wait_for(
            client.translate(text, src='auto', dest=language), timeout=timeout
        )
        if result._response.status_code != 200:
            raise ValueError('翻译服务没有返回 HTTP 200')
        return result.text


def _retryable(error):
    # Rate limits and authentication failures are terminal for this invocation.
    status = re.search(r'(?:status(?: code)?|HTTP)[^0-9]*(\d{3})', str(error), re.I)
    if status:
        return int(status.group(1)) == 408 or int(status.group(1)) >= 500
    return isinstance(error, (TimeoutError, OSError)) or any(
        word in type(error).__name__.lower() for word in ('timeout', 'connection', 'transport')
    )


def _engine_worker(output, engine, text, language, timeout):
    """Importable Windows/PyInstaller spawn target; never touches Qt widgets."""
    try:
        if engine == 'translators':
            import translators as ts
            language = 'zh' if language == 'zh-cn' else language
            result = ts.translate_text(
                text, translator='bing', from_language='auto', to_language=language,
                timeout=timeout,
            )
        elif engine == 'googletrans':
            result = asyncio.run(_google_translate(text, language, timeout))
        else:
            raise ValueError('未知翻译引擎')
        if not isinstance(result, str) or not result.strip():
            raise ValueError('翻译服务返回空结果或无效结果')
        output.put((result, '', False))
    except Exception as error:
        output.put((None, f'{type(error).__name__}: {str(error)[:300]}', _retryable(error)))


def _run_engine(engine, text, language, timeout):
    context = multiprocessing.get_context('spawn')
    output = context.Queue(maxsize=1)
    process = context.Process(
        target=_engine_worker, args=(output, engine, text, language, timeout), daemon=True
    )
    started = False
    deadline = time.monotonic() + timeout
    try:
        process.start()
        started = True
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, f'翻译超时（{timeout:.1f} 秒，含依赖加载）', True
            try:
                return output.get(timeout=min(0.1, remaining))
            except queue.Empty:
                if not process.is_alive():
                    try:
                        return output.get(timeout=min(0.1, max(0.001, deadline - time.monotonic())))
                    except queue.Empty:
                        return None, f'翻译子进程异常退出（{process.exitcode}）', False
    except Exception as error:
        return None, f'无法启动翻译引擎：{type(error).__name__}: {error}', False
    finally:
        if started:
            if process.is_alive():
                process.terminate()
            process.join(0.5)
            if process.is_alive():
                process.kill()
                process.join(0.5)
        if not started or not process.is_alive():
            process.close()
        output.close()
        output.join_thread()


def _translate_one(engine, text, target_lang, verbose, timeout):
    language, timeout = _arguments(text, target_lang, timeout)
    if not text.strip():
        return text
    if not _available(engine):
        LOGGER.warning('%s：%s', engine, DEPENDENCY_HINT)
        return None
    result, error, _ = _run_engine(engine, text, language, timeout)
    if error:
        LOGGER.warning('%s 翻译失败：%s', engine, error)
    elif verbose:
        LOGGER.debug('%s 翻译成功', engine)
    return result


def translators_translate(text, target_lang='en', verbose=False, timeout=10):
    """Translate through Bing using translators; return text or None."""
    return _translate_one('translators', text, target_lang, verbose, timeout)


def googletrans_translate(text, target_lang='en', verbose=False, timeout=10):
    """Adapt Googletrans 4.0.2's async API to a bounded synchronous call."""
    return _translate_one('googletrans', text, target_lang, verbose, timeout)


def translate_auto(text, target_lang='en', delay=0.5, verbose=False, timeout=30, max_attempts=3):
    """Try available engines, with one total deadline for imports and retries."""
    language, timeout = _arguments(text, target_lang, timeout)
    delay = float(delay)
    if not math.isfinite(delay) or delay < 0:
        raise ValueError('重试间隔必须是有限的非负数')
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 5:
        raise ValueError('重试轮数必须为 1 到 5 的整数')
    if not text.strip():
        return text
    engines = [engine for engine in ('translators', 'googletrans') if _available(engine)]
    if not engines:
        LOGGER.warning(DEPENDENCY_HINT)
        return None
    deadline = time.monotonic() + timeout
    errors = []
    for attempt in range(max_attempts):
        retry = []
        for index, engine in enumerate(engines):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # Reserve time for fallback even when the primary import hangs.
            budget = min(10.0, remaining / (len(engines) - index))
            result, error, can_retry = _run_engine(engine, text, language, budget)
            if result is not None:
                return result
            errors.append(f'{engine}: {error}')
            if verbose:
                LOGGER.debug('第 %d 轮，%s 失败：%s', attempt + 1, engine, error)
            if can_retry:
                retry.append(engine)
        if not retry or attempt + 1 == max_attempts or time.monotonic() >= deadline:
            break
        engines = retry
        time.sleep(min(delay * (2 ** attempt), max(0, deadline - time.monotonic())))
    LOGGER.warning('自动翻译失败：%s', '; '.join(errors[-2:]))
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description='有超时保护的自动翻译')
    parser.add_argument('text', nargs='?', default='人工智能正在改变世界。')
    parser.add_argument('--to', default='en', help='目标语言，例如 en、zh-cn')
    parser.add_argument('--timeout', type=float, default=30, help='总超时秒数，默认 30')
    parser.add_argument('--attempts', type=int, default=3, help='最多尝试轮数，默认 3')
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    try:
        result = translate_auto(args.text, args.to, verbose=args.verbose,
                                timeout=args.timeout, max_attempts=args.attempts)
    except KeyboardInterrupt:
        print('已取消（用户中断）', file=sys.stderr)
        return 130
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    if result is None:
        print('最终失败：未获取到有效译文，请检查网络或配置翻译 API。', file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == '__main__':
    multiprocessing.freeze_support()
    raise SystemExit(main())
