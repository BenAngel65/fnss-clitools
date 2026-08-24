"""fnsssync — unified sync command for all fnss-clitools submodules.

Iterates through every registered module (oinbox, odiary, onote) and calls
their `push_pending(client)` (and onote's `reconcile_local_cache`) so that
a single command syncs everything offline-first.

Exit codes:
  0  everything succeeded (or nothing to sync)
  1  configuration missing
  2  client construction failed
  3  at least one module had a fatal error
"""
from __future__ import annotations

import importlib
import sys
from typing import Callable, List, Tuple

from .config import is_configured, load_config
from .fnss import FnssClient, FnssError
from .render import (
    render_error,
    render_info,
    render_success,
    render_warning,
)


# Each entry describes one module's sync surface.
# - name:    display label
# - module:  import path
# - push_fn: function name that returns either int (pushed) or (pushed, errors)
# - result_kind: "int" or "tuple" — how to interpret the return value
# - pending_loader: function returning a list (for diagnostic display)
# - extras: optional follow-up calls (e.g. onote reconcile_local_cache)
_MODULES = [
    {
        "name": "oinbox",
        "module": "oinbox.sync",
        "push_fn": "push_pending",
        "result_kind": "tuple",  # oinbox.push_pending returns (int, list[str])
        "pending_loader": "_load_pending",
        "extras": [],
    },
    {
        "name": "odiary",
        "module": "odiary.sync",
        "push_fn": "push_pending",
        "result_kind": "tuple",  # odiary.push_pending returns (int, list[str])
        "pending_loader": "load_pending",
        "extras": [],
    },
    {
        "name": "onote",
        "module": "onote.sync",
        "push_fn": "push_pending",
        "result_kind": "tuple",
        "pending_loader": "load_pending",
        "extras": [
            ("reconcile_local_cache", "tuple", "vault"),
        ],
    },
    {
        "name": "fnsswatch",
        "module": "fnsswatch.sync",
        "push_fn": "push_pending",
        "result_kind": "tuple",
        "pending_loader": "load_pending",
        "extras": [],
    },
]


def _load_callable(module_path: str, fn_name: str) -> Callable:
    mod = importlib.import_module(module_path)
    return getattr(mod, fn_name)


def _load_pending_count(mod_spec: dict) -> int:
    """Try to read the pending list count for diagnostic. Best-effort."""
    try:
        mod = importlib.import_module(mod_spec["module"])
        loader = getattr(mod, mod_spec["pending_loader"], None)
        if loader is None:
            return 0
        return len(loader() or [])
    except Exception:
        return 0


def _call(func: Callable, client: FnssClient, result_kind: str, extra_args=()):
    """Invoke a push/reconcile function and normalize its result.

    Returns: (pushed_count, error_list)
    """
    try:
        result = func(client, *extra_args)
    except FnssError as e:
        return 0, [f"FnssError: {e}"]
    except Exception as e:
        return 0, [f"{type(e).__name__}: {e}"]
    if result_kind == "int":
        return int(result or 0), []
    if result_kind == "tuple":
        if isinstance(result, tuple):
            pushed = int(result[0] or 0)
            errs = list(result[1]) if len(result) > 1 else []
            return pushed, errs
        return 0, [f"unexpected return type: {type(result).__name__}"]
    return 0, [f"unknown result_kind: {result_kind}"]


def main(argv=None) -> int:
    cfg = load_config()
    if not is_configured(cfg):
        render_error("未配置 fnss 凭证，运行 `onote config --host ... --token ...` 设置")
        return 1

    try:
        client = FnssClient(cfg["host"], cfg["token"])
    except FnssError as e:
        render_error(str(e))
        return 2

    render_info(f"开始同步 vault={cfg['vault']!r}")

    # Pre-flight: show pending counts so user sees what's queued.
    pre_counts: dict[str, int] = {}
    for mod_spec in _MODULES:
        pre_counts[mod_spec["name"]] = _load_pending_count(mod_spec)
    if any(v > 0 for v in pre_counts.values()):
        summary = ", ".join(f"{n}={c}" for n, c in pre_counts.items() if c > 0)
        render_info(f"  pending: {summary}")
    else:
        render_info("  pending: 无")

    total_pushed = 0
    total_errors: List[str] = []
    any_fatal = False

    for mod_spec in _MODULES:
        name = mod_spec["name"]
        pending_before = pre_counts[name]
        try:
            push_func = _load_callable(mod_spec["module"], mod_spec["push_fn"])
        except (ImportError, AttributeError) as e:
            render_warning(f"{name}: 模块加载失败 — {e}")
            any_fatal = True
            continue

        pushed, errs = _call(push_func, client, mod_spec["result_kind"])
        total_pushed += pushed
        total_errors.extend(errs)

        # Run extras (e.g. onote reconcile_local_cache)
        for extra_fn_name, extra_kind, extra_arg_key in mod_spec.get("extras", []):
            try:
                extra_func = _load_callable(mod_spec["module"], extra_fn_name)
            except (ImportError, AttributeError) as e:
                render_warning(f"{name}.{extra_fn_name}: 模块加载失败 — {e}")
                any_fatal = True
                continue
            extra_args = (cfg[extra_arg_key],) if extra_arg_key else ()
            extra_pushed, extra_errs = _call(
                extra_func, client, extra_kind, extra_args
            )
            total_pushed += extra_pushed
            total_errors.extend(extra_errs)
            pushed += extra_pushed
            errs.extend(extra_errs)
            # Annotate the message sources
            for e in extra_errs:
                total_errors[-1] = f"{name}.{extra_fn_name}: {e}"

        # Status reporting: distinguish 3 cases
        if pushed > 0:
            render_success(f"{name}: 推送 {pushed} 条")
        elif errs:
            render_warning(
                f"{name}: 推送失败（{len(errs)} 条错误，仍待同步）"
            )
        elif pending_before > 0:
            # Had pending but push returned 0 → must be idempotent
            # (all entries already on server) or saved locally by another path
            render_info(
                f"{name}: 无新推送（{pending_before} 条已在 server，idempotent 跳过）"
            )
        else:
            render_info(f"{name}: 无待同步项")

    print()
    if total_pushed > 0:
        render_success(f"总计推送 {total_pushed} 条")
    if total_errors:
        for e in total_errors[:10]:
            render_warning(f"  · {e}")
        if len(total_errors) > 10:
            render_warning(f"  · ... 还有 {len(total_errors) - 10} 条错误")
        any_fatal = True

    return 3 if any_fatal else 0


if __name__ == "__main__":
    sys.exit(main())