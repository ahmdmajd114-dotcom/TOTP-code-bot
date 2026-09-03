"""Pure catalog matching and customer-facing formatting helpers."""

from __future__ import annotations

import re
from typing import Iterable, Mapping

from intent_fallback import normalize_arabic_text


CATALOG_CATEGORY_PREFIX = "catalog:"
CHATGPT_OWNER_SHORTCUT = "ج"
CHATGPT_PRODUCT_TERMS = {
    "chatgpt", "chat", "gpt", "جات", "شات", "چات", "تشات", "جيبيتي", "شات جي بي تي",
}


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


def find_owner_catalog_shortcut(
    text: str, products: Iterable[Mapping[str, object]]
) -> Mapping[str, object] | None:
    """Resolve an exact owner shortcut to one active catalog product.

    The one-letter Arabic shortcut "ج" is reserved for the ChatGPT product.
    Every other product must be written as its complete configured name or alias,
    so normal owner messages never accidentally send an offer to a customer.
    """
    normalized = normalize_arabic_text(text)
    active_products = [product for product in products if product.get("is_active")]

    if normalized == CHATGPT_OWNER_SHORTCUT:
        chatgpt_products = []
        for product in active_products:
            terms = [str(product.get("name") or "")]
            terms.extend(str(alias) for alias in (product.get("aliases") or []))
            normalized_terms = {normalize_arabic_text(term) for term in terms}
            compact_terms = {term.replace(" ", "") for term in normalized_terms}
            if normalized_terms & CHATGPT_PRODUCT_TERMS or compact_terms & {"chatgpt", "شاتجيبيتي"}:
                chatgpt_products.append(product)
        return chatgpt_products[0] if len(chatgpt_products) == 1 else None

    matches = []
    for product in active_products:
        terms = [str(product.get("name") or "")]
        terms.extend(str(alias) for alias in (product.get("aliases") or []))
        if normalized in {normalize_arabic_text(term) for term in terms}:
            matches.append(product)
    return matches[0] if len(matches) == 1 else None


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

    lines = [f"بلي موجود، عدنا باقات {product.get('name')} التالية:", ""]
    for plan in active_plans:
        title = re.sub(r"^اشتراك\s+", "", str(plan.get("name") or "").strip()).strip()
        duration = str(plan.get("duration") or "").strip()
        normalized_title = normalize_arabic_text(title)
        account_type = "خاص" if "خاص" in normalized_title else "مشترك" if "مشترك" in normalized_title else ""
        simple_plan_words = set(normalized_title.split()) <= {"شهر", "شهرين", "خاص", "مشترك"}
        if duration and account_type and simple_plan_words:
            title = f"{duration} {account_type}"
        elif duration and normalize_arabic_text(duration) not in normalized_title:
            title = f"{title} لمدة {duration}" if title else duration

        raw_price = plan.get("price")
        try:
            numeric_price = float(raw_price)
            if numeric_price >= 1000:
                numeric_price /= 1000
            shown_price = int(numeric_price) if numeric_price.is_integer() else numeric_price
            unit = "آلاف" if 3 <= numeric_price <= 10 else "ألف"
            price_text = f"{shown_price} {unit}"
        except (TypeError, ValueError):
            price_text = str(raw_price or "").strip()

        line = f"- {title}، سعره {price_text}."
        if plan.get("description"):
            line += f"\n  ملاحظة: {str(plan['description']).strip()}"
        lines.append(line)
    return "\n".join(lines)
