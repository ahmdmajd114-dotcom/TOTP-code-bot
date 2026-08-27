"""Pure catalog matching and customer-facing formatting helpers."""

from __future__ import annotations

import re
from typing import Iterable, Mapping

from intent_fallback import normalize_arabic_text


CATALOG_CATEGORY_PREFIX = "catalog:"


def catalog_category(product_id: object) -> str:
    return f"{CATALOG_CATEGORY_PREFIX}{product_id}"


def catalog_product_id(category: str) -> str | None:
    if not category.startswith(CATALOG_CATEGORY_PREFIX):
        return None
    product_id = category[len(CATALOG_CATEGORY_PREFIX):].strip()
    return product_id or None


def match_catalog_products(text: str, products: Iterable[Mapping[str, object]]) -> list[Mapping[str, object]]:
    """Match active products by full normalized name/alias, ordered by appearance."""
    normalized = normalize_arabic_text(text)
    matches: list[tuple[int, Mapping[str, object]]] = []
    for product in products:
        if not product.get("is_active"):
            continue
        terms = [str(product.get("name") or "")]
        terms.extend(str(alias) for alias in (product.get("aliases") or []))
        positions: list[int] = []
        for term in terms:
            candidate = normalize_arabic_text(term)
            if not candidate:
                continue
            pattern = rf"(?<!\w){re.escape(candidate)}(?!\w)"
            found = re.search(pattern, normalized)
            if found:
                positions.append(found.start())
        if positions:
            matches.append((min(positions), product))
    matches.sort(key=lambda item: item[0])
    return [product for _, product in matches]


def format_customer_catalog_reply(
    product: Mapping[str, object],
    plans: Iterable[Mapping[str, object]],
) -> str | None:
    """Render only currently active catalog data; never fall back to fixed prices."""
    if not product.get("is_active"):
        return None
    active_plans = [plan for plan in plans if plan.get("is_active")]
    if not active_plans:
        return None

    lines = [f"بلي موجود، هاي الباقات المتوفرة {product.get('name')}:", ""]
    for plan in active_plans:
        details = [str(plan.get("name") or "").strip(), str(plan.get("price"))]
        if plan.get("duration"):
            details.append(str(plan["duration"]).strip())
        line = " — ".join(value for value in details if value)
        if plan.get("description"):
            line += f" — {str(plan['description']).strip()}"
        lines.append(f"- {line}")
    return "\n".join(lines)
