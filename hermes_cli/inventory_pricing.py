"""Pure per-model pricing presentation for the shared inventory builder."""

from __future__ import annotations


def format_pricing_entries(models, raw_pricing, slug, _format_price_per_mtok, compute_sale_discount):
    """Format rows using the exact helper bindings captured by the caller."""
    formatted: dict[str, dict] = {}
    for mid in models:
        p = raw_pricing.get(mid)
        if not p:
            continue
        inp_raw, out_raw = p.get("prompt", ""), p.get("completion", "")
        cache_raw = p.get("input_cache_read", "")
        inp = _format_price_per_mtok(inp_raw) if inp_raw != "" else ""
        out = _format_price_per_mtok(out_raw) if out_raw != "" else ""
        entry: dict = {
            "input": inp, "output": out,
            "cache": _format_price_per_mtok(cache_raw) if cache_raw else None,
            "free": inp == "free" and out in ("free", ""),  # both input and output cost nothing
        }
        # Sale chrome is Nous Portal-only (other catalogs' nested pricing.original is ignored); free
        # models get flat -100% chrome, was_* only when the gateway served an original.
        if slug == "nous":
            sale = compute_sale_discount(inp_raw, out_raw, p.get("original"))
            if sale is not None:
                discount_percent, was_prompt_raw, was_out_raw = sale
                entry["discount_percent"] = discount_percent
                for key, was_raw in (("was_input", was_prompt_raw), ("was_output", was_out_raw)):
                    if was_raw != "":
                        entry[key] = _format_price_per_mtok(was_raw)
        formatted[mid] = entry
    return formatted
