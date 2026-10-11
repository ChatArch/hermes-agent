"""Import-safe Radon JSON aggregation for static code metrics."""
import json
import os
import shutil
import subprocess
import sys

def radon(files, tree):
    """Aggregate cc/mi JSON with independent 120s subprocess budgets.

    subprocess.run handles its direct child on timeout, not arbitrary descendants.
    """
    R = shutil.which("radon") or os.path.join(os.path.dirname(sys.executable), "radon")
    out = {}
    # radon can't take a list file; run per-directory roots instead
    roots = sorted({os.path.relpath(f, tree).split(os.sep)[0] if os.sep in os.path.relpath(f, tree) else os.path.relpath(f, tree) for f in files})
    roots = [r for r in roots if r != "tests"]
    try:
        cc = subprocess.run([R, "cc", "-j", "-s", "-e", "tests/*,tests/**", *roots], cwd=tree, capture_output=True, text=True, check=False, timeout=120).stdout
        ccj = json.loads(cc); blocks = [b for v in ccj.values() if isinstance(v, list) for b in v if isinstance(b, dict) and "complexity" in b]
        cs = sorted(b["complexity"] for b in blocks)
        out.update({"cc_blocks": len(cs), "cc_avg": round(sum(cs) / max(1, len(cs)), 2), "cc_p95": cs[int(len(cs) * .95) - 1] if cs else 0, "cc_max": cs[-1] if cs else 0,
                    "cc_gt_15": sum(1 for c in cs if c > 15), "cc_gt_30": sum(1 for c in cs if c > 30), "cc_gt_50": sum(1 for c in cs if c > 50),
                    "cc_worst": sorted(((b["complexity"], f, b["name"]) for f, v in ccj.items() if isinstance(v, list) for b in v if isinstance(b, dict) and "complexity" in b), reverse=True)[:8]})
    except subprocess.TimeoutExpired: out["cc_error"] = "timeout after 120s"
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError) as e: out["cc_error"] = repr(e)[:200]
    try:
        mi = subprocess.run([R, "mi", "-j", "-e", "tests/*,tests/**", *roots], cwd=tree, capture_output=True, text=True, check=False, timeout=120).stdout
        mij = json.loads(mi); vals = [v["mi"] for v in mij.values() if isinstance(v, dict) and "mi" in v]
        out.update({"mi_files": len(vals), "mi_avg": round(sum(vals) / max(1, len(vals)), 2), "mi_lt_20": sum(1 for v in vals if v < 20), "mi_lt_10": sum(1 for v in vals if v < 10)})
    except subprocess.TimeoutExpired: out["mi_error"] = "timeout after 120s"
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError) as e: out["mi_error"] = repr(e)[:200]
    return out
