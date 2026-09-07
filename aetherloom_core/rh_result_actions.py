"""Delete only the local outputs owned by one finished RunningHub result card."""

from dataclasses import dataclass
import os
from pathlib import Path


class ResultDeletionError(ValueError):
    pass


@dataclass(frozen=True)
class DeletionPlan:
    paths: tuple
    missing: tuple


@dataclass(frozen=True)
class DeletionResult:
    removed: tuple
    missing: tuple
    failures: tuple

    @property
    def complete(self):
        return not self.failures


def card_is_active(window, card):
    if bool(getattr(card, '_rh_run_inflight', False)):
        return True
    task_id = str(getattr(card, '_task_id', '') or '')
    if task_id and task_id in (getattr(window, '_rh_live_task_ids', set()) or ()):
        return True
    if task_id and task_id in (getattr(window, '_rh_recovering_tasks', set()) or ()):
        return True
    if task_id and any(task_id in (tasks or ()) for tasks in (getattr(window, '_rh_running_tasks', {}) or {}).values()):
        return True
    return any(isinstance(entry, dict) and entry.get('card') is card
               for entry in (getattr(window, '_rh_retry_queue', []) or []))


def associated_paths(card):
    if hasattr(card, '_rh_output_files'):
        candidates = getattr(card, '_rh_output_files') or ()
    else:
        candidates = [record.get('path') for record in (getattr(card, '_outputs', ()) or ())
                      if isinstance(record, dict)]
    if isinstance(candidates, (str, os.PathLike)):
        candidates = (candidates,)
    return tuple(dict.fromkeys(os.fspath(path) for path in candidates if path))


def _canonical(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))


def plan_card_deletion(card, *, fallback_roots=(), fallback_inputs=()):
    roots = getattr(card, '_rh_output_roots', fallback_roots) or ()
    if isinstance(roots, (str, os.PathLike)):
        roots = (roots,)
    roots = tuple(_canonical(root) for root in roots if root)
    inputs = getattr(card, '_rh_input_files', ()) or ()
    if isinstance(inputs, (str, os.PathLike)):
        inputs = (inputs,)
    if isinstance(fallback_inputs, (str, os.PathLike)):
        fallback_inputs = (fallback_inputs,)
    inputs = tuple(inputs) + tuple(fallback_inputs)
    protected = {_canonical(path) for path in inputs if path}
    paths, missing, seen = [], [], set()
    for raw in associated_paths(card):
        if not os.path.isabs(raw):
            raise ResultDeletionError('关联文件路径不是绝对路径，已停止删除。')
        path = Path(raw)
        canonical = _canonical(path)
        if canonical in seen:
            continue
        seen.add(canonical)
        if canonical in protected:
            raise ResultDeletionError(f'关联记录包含输入文件，已停止删除：{path.name}')
        if {'.rh_downloads', '.rh_decoded'}.intersection(part.lower() for part in path.parts):
            raise ResultDeletionError('下载或解码凭据不属于可删除的卡片输出。')
        contained = False
        for root in roots:
            try:
                contained = canonical != root and os.path.commonpath((canonical, root)) == root
            except ValueError:
                contained = False
            if contained:
                break
        if not contained:
            raise ResultDeletionError(f'文件不在该卡片的输出目录中，已停止删除：{path.name}')
        if path.is_symlink() or path.is_dir():
            raise ResultDeletionError(f'不能通过卡片删除目录或链接：{path.name}')
        if not path.exists():
            missing.append(str(path))
        elif not path.is_file():
            raise ResultDeletionError(f'关联对象不是普通文件，已停止删除：{path.name}')
        else:
            paths.append(str(path))
    return DeletionPlan(tuple(paths), tuple(missing))


def delete_card_files(card, move_to_trash, *, is_active=lambda: False,
                      fallback_roots=(), fallback_inputs=()):
    """Validate the whole association before moving individual files to trash.

    A failed or interrupted removal leaves the card available for another try.
    Neither folders nor the download-receipt directory are deletion targets.
    """
    if is_active():
        raise ResultDeletionError('任务仍在运行，请先取消任务并等待取消完成。')
    plan = plan_card_deletion(card, fallback_roots=fallback_roots, fallback_inputs=fallback_inputs)
    removed, missing, failures = [], list(plan.missing), []
    for index, path in enumerate(plan.paths):
        if is_active():
            failures.extend((remaining, '任务仍在运行，已停止删除') for remaining in plan.paths[index:])
            break
        try:
            # Re-check immediately before the existing recycle-bin helper runs.
            current = Path(path)
            if current.is_symlink() or current.is_dir():
                raise ResultDeletionError('关联文件已变为目录或链接')
            if not current.exists():
                missing.append(path)
                continue
            count, errors = move_to_trash([path])
            if errors or current.exists() or not count:
                message = '; '.join(str(error[-1] if isinstance(error, (tuple, list)) else error)
                                    for error in (errors or ())) or '文件仍存在，未能完成删除'
                failures.append((path, message))
            else:
                removed.append(path)
        except Exception as exc:
            failures.append((path, str(exc)))
    return DeletionResult(tuple(removed), tuple(missing), tuple(failures))
