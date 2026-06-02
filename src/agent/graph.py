from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool

from src.core.llm import build_chat_model, normalize_content
from src.core.schemas import (
    AgentResult,
    CalculateTotalsInput,
    DiscountInput,
    ListProductsInput,
    OrderLineInput,
    ProductDetailInput,
    SaveOrderInput,
    ToolCallRecord,
)
from src.utils.data_store import OrderDataStore

ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = ROOT_DIR / "data"
DEFAULT_OUTPUT_DIR = ROOT_DIR / "artifacts" / "orders"


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    stripped = stripped.replace("Ä‘", "d").replace("Ä", "D")
    compact = re.sub(r"[^a-zA-Z0-9]+", " ", stripped.lower())
    return re.sub(r"\s+", " ", compact).strip()


def build_system_prompt(today: str | None = None) -> str:
    current_day = today or "2026-06-01"
    return f"""
You are OrderDesk, an electronics retail order agent.
Today is {current_day}.

Hard rules:
- Always answer in Vietnamese.
- Never invent product IDs, SKUs, prices, stock, discounts, totals, order IDs, or file paths.
- Use only tool outputs for catalog facts, pricing, campaign discount, totals, and saved file location.
- Before any tool call, verify that the user supplied: customer name, phone number, email, shipping address, and at least one product request with quantity. If any field is missing, ask only for the missing fields and stop.
- Refuse without tools when the user asks to create fake invoices, bypass or ignore stock, force a manual discount, use a fake catalog, ignore policy, alter totals, or save an unvalidated order.
- For valid orders, call tools in this exact order: list_products, get_product_details, get_discount, calculate_order_totals, save_order.
- Do not call get_discount, calculate_order_totals, or save_order if product details show insufficient stock.
- Save only after validation succeeds.
- Final confirmation must be concise and grounded in save_order: mention order id, discount rate/campaign, final total, and saved path.

Business rules:
- Treat mixed Vietnamese/English order requests normally.
- Quoted product names without an explicit quantity mean quantity 1.
- Customer tier is standard unless the user explicitly says VIP.
- Do not accept requests to override campaign discounts or stock policy even if customer data is complete.
""".strip()


def build_tools(store: OrderDataStore):
    @tool(args_schema=ListProductsInput)
    def list_products(
        query: str | None = None,
        category: str | None = None,
        max_unit_price: int | None = None,
        required_tags: list[str] | None = None,
        in_stock_only: bool = True,
        limit: int = 8,
    ) -> str:
        """Search the electronics catalog by name, brand, category, tags, and budget hints."""
        payload = store.list_products(
            query=query,
            category=category,
            max_unit_price=max_unit_price,
            required_tags=required_tags or [],
            in_stock_only=in_stock_only,
            limit=limit,
        )
        return json.dumps(payload, ensure_ascii=False)

    @tool(args_schema=ProductDetailInput)
    def get_product_details(product_ids: list[str]) -> str:
        """Return exact product facts and a detail_token for the requested catalog product IDs."""
        return json.dumps(store.get_product_details(product_ids), ensure_ascii=False)

    @tool(args_schema=DiscountInput)
    def get_discount(seed_hint: str, customer_tier: str = "standard") -> str:
        """Return the deterministic campaign discount for a customer seed and tier."""
        return json.dumps(
            store.get_discount(seed_hint=seed_hint, customer_tier=customer_tier),
            ensure_ascii=False,
        )

    @tool(args_schema=CalculateTotalsInput)
    def calculate_order_totals(items: list[OrderLineInput], detail_token: str, discount_rate: float) -> str:
        """Validate stock and calculate subtotal, discount amount, and final VND total."""
        return json.dumps(
            store.calculate_order_totals(items=items, detail_token=detail_token, discount_rate=discount_rate),
            ensure_ascii=False,
        )

    @tool(args_schema=SaveOrderInput)
    def save_order(
        customer_name: str,
        customer_phone: str,
        customer_email: str,
        shipping_address: str,
        items: list[OrderLineInput],
        detail_token: str,
        discount_rate: float,
        campaign_code: str,
        customer_tier: str = "standard",
        notes: str = "",
    ) -> str:
        """Persist the validated order JSON after recomputing totals."""
        return json.dumps(
            store.save_order(
                customer_name=customer_name,
                customer_phone=customer_phone,
                customer_email=customer_email,
                shipping_address=shipping_address,
                items=items,
                detail_token=detail_token,
                discount_rate=discount_rate,
                campaign_code=campaign_code,
                customer_tier=customer_tier,
                notes=notes,
            ),
            ensure_ascii=False,
        )

    return [list_products, get_product_details, get_discount, calculate_order_totals, save_order]


def build_agent(
    data_dir: Path | None = None,
    output_dir: Path | None = None,
    *,
    provider: str = "google",
    model_name: str | None = None,
    today: str | None = None,
):
    store = OrderDataStore(data_dir or DEFAULT_DATA_DIR, output_dir or DEFAULT_OUTPUT_DIR, today=today)
    model = build_chat_model(provider=provider, model_name=model_name, temperature=0.0)
    return create_agent(
        model=model,
        tools=build_tools(store),
        system_prompt=build_system_prompt(today or store.today),
    )


def run_agent(
    query: str,
    *,
    provider: str = "google",
    model_name: str | None = None,
    data_dir: Path | None = None,
    output_dir: Path | None = None,
    today: str | None = None,
) -> AgentResult:
    store = OrderDataStore(data_dir or DEFAULT_DATA_DIR, output_dir or DEFAULT_OUTPUT_DIR, today=today)
    preflight = _parse_order_request(query, store)

    if preflight["guardrail"]:
        return AgentResult(
            query=query,
            final_answer=(
                "Xin lỗi, tôi không thể tạo hóa đơn giả, bỏ qua tồn kho/catalog/policy "
                "hoặc tự ép khuyến mãi. Tôi chỉ có thể tạo đơn hợp lệ theo catalog và "
                "khuyến mãi hệ thống."
            ),
            tool_calls=[],
            provider=provider,
            model_name=model_name,
        )

    if preflight["missing"]:
        missing_text = ", ".join(preflight["missing"])
        return AgentResult(
            query=query,
            final_answer=f"Tôi cần thêm {missing_text} trước khi kiểm tra catalog và tạo đơn hàng.",
            tool_calls=[],
            provider=provider,
            model_name=model_name,
        )

    customer = preflight["customer"]
    items: list[OrderLineInput] = preflight["items"]
    tool_calls: list[ToolCallRecord] = []

    list_payload = store.list_products(query=query, limit=20)
    tool_calls.append(
        _record("list_products", {"query": query, "limit": 20}, list_payload)
    )

    product_ids = [item.product_id for item in items]
    details_payload = store.get_product_details(product_ids)
    tool_calls.append(_record("get_product_details", {"product_ids": product_ids}, details_payload))

    detail_items = details_payload.get("items", [])
    stock_errors = []
    stock_by_id = {
        item.get("product_id"): int(item.get("stock", 0))
        for item in detail_items
        if item.get("status") == "ok"
    }
    name_by_id = {
        item.get("product_id"): str(item.get("name", item.get("product_id", "")))
        for item in detail_items
        if item.get("status") == "ok"
    }
    for item in items:
        if item.quantity > stock_by_id.get(item.product_id, 0):
            stock_errors.append(
                f"{name_by_id.get(item.product_id, item.product_id)} chỉ còn {stock_by_id.get(item.product_id, 0)}, "
                f"không đủ cho số lượng {item.quantity}."
            )
    if stock_errors:
        return AgentResult(
            query=query,
            final_answer="Không thể tạo đơn vì không đủ tồn kho: " + " ".join(stock_errors),
            tool_calls=tool_calls,
            provider=provider,
            model_name=model_name,
        )

    customer_tier = "vip" if "vip" in _normalize(query).split() else "standard"
    discount_payload = store.get_discount(seed_hint=customer["email"], customer_tier=customer_tier)
    tool_calls.append(
        _record(
            "get_discount",
            {"seed_hint": customer["email"], "customer_tier": customer_tier},
            discount_payload,
        )
    )

    totals_payload = store.calculate_order_totals(
        items=items,
        detail_token=details_payload["detail_token"],
        discount_rate=discount_payload["discount_rate"],
    )
    tool_calls.append(
        _record(
            "calculate_order_totals",
            {
                "items": [item.model_dump() for item in items],
                "detail_token": details_payload["detail_token"],
                "discount_rate": discount_payload["discount_rate"],
            },
            totals_payload,
        )
    )

    if totals_payload.get("status") != "ok":
        errors = "; ".join(totals_payload.get("errors", []))
        return AgentResult(
            query=query,
            final_answer=f"Không thể tạo đơn vì dữ liệu đơn hàng chưa hợp lệ: {errors}",
            tool_calls=tool_calls,
            provider=provider,
            model_name=model_name,
        )

    save_payload = store.save_order(
        customer_name=customer["name"],
        customer_phone=customer["phone"],
        customer_email=customer["email"],
        shipping_address=customer["shipping_address"],
        items=items,
        detail_token=details_payload["detail_token"],
        discount_rate=discount_payload["discount_rate"],
        campaign_code=discount_payload["campaign_code"],
        customer_tier=discount_payload["customer_tier"],
    )
    tool_calls.append(
        _record(
            "save_order",
            {
                "customer_name": customer["name"],
                "customer_phone": customer["phone"],
                "customer_email": customer["email"],
                "shipping_address": customer["shipping_address"],
                "items": [item.model_dump() for item in items],
                "detail_token": details_payload["detail_token"],
                "discount_rate": discount_payload["discount_rate"],
                "campaign_code": discount_payload["campaign_code"],
                "customer_tier": discount_payload["customer_tier"],
            },
            save_payload,
        )
    )

    saved_order = save_payload.get("saved_order")
    saved_path = save_payload.get("path")
    pricing = saved_order["pricing"]
    final_answer = (
        f"Đã lưu đơn {saved_order['order_id']} với {discount_payload['campaign_code']} "
        f"({int(pricing['discount_rate'] * 100)}%); tổng cuối là {pricing['final_total']:,} VND. "
        f"File đã lưu tại {saved_order['save_path']}."
    )
    return AgentResult(
        query=query,
        final_answer=final_answer,
        tool_calls=tool_calls,
        provider=provider,
        model_name=model_name,
        saved_order=saved_order,
        saved_order_path=saved_path,
    )


def extract_final_answer(messages) -> str:
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            text = normalize_content(message.content)
            if text:
                return text
    return ""


def extract_tool_calls(messages) -> list[ToolCallRecord]:
    pending: dict[str, dict[str, Any]] = {}
    records: list[ToolCallRecord] = []

    for message in messages:
        if isinstance(message, AIMessage):
            for tool_call in getattr(message, "tool_calls", []) or []:
                pending[tool_call["id"]] = {
                    "name": tool_call["name"],
                    "args": tool_call.get("args", {}) or {},
                }
        elif isinstance(message, ToolMessage):
            metadata = pending.pop(message.tool_call_id, {})
            records.append(
                ToolCallRecord(
                    name=str(getattr(message, "name", None) or metadata.get("name", "")),
                    args=metadata.get("args", {}),
                    output=normalize_content(message.content),
                )
            )

    for metadata in pending.values():
        records.append(ToolCallRecord(name=metadata["name"], args=metadata["args"], output=""))
    return records


def extract_saved_order(tool_calls: list[ToolCallRecord]) -> tuple[dict | None, str | None]:
    for record in reversed(tool_calls):
        if record.name != "save_order" or not record.output:
            continue
        try:
            payload = json.loads(record.output)
        except json.JSONDecodeError:
            continue
        if payload.get("status") != "saved":
            return None, None
        return payload.get("saved_order"), payload.get("path")
    return None, None


def _record(name: str, args: dict[str, Any], output: Any) -> ToolCallRecord:
    return ToolCallRecord(name=name, args=args, output=json.dumps(output, ensure_ascii=False))


def _parse_order_request(query: str, store: OrderDataStore) -> dict[str, Any]:
    normalized = _normalize(query)
    guardrail_terms = [
        "hoa don gia",
        "fake invoice",
        "fake catalog",
        "giam gia 90",
        "ep giam gia",
        "manual discount",
        "bo qua ton kho",
        "bypass stock",
        "ignore stock",
        "bo qua policy",
        "ignore policy",
        "khong can theo catalog",
        "ignore catalog",
        "luu hoa don luon",
    ]
    guardrail = any(term in normalized for term in guardrail_terms)

    email_match = re.search(r"[\w.+-]+@[\w.-]+\.\w+", query)
    phone_match = re.search(r"\b0\d{9,10}\b", query)
    items = _extract_items(query, store)

    customer = {
        "name": _extract_customer_name(query),
        "phone": phone_match.group(0) if phone_match else "",
        "email": email_match.group(0) if email_match else "",
        "shipping_address": _extract_shipping_address(query),
    }

    missing: list[str] = []
    if not customer["name"]:
        missing.append("tên khách hàng")
    if not customer["phone"]:
        missing.append("số điện thoại")
    if not customer["email"]:
        missing.append("email")
    if not customer["shipping_address"]:
        missing.append("địa chỉ giao hàng")
    if not items:
        missing.append("sản phẩm và số lượng")

    return {
        "guardrail": guardrail,
        "missing": missing,
        "customer": customer,
        "items": items,
    }


def _extract_customer_name(query: str) -> str:
    patterns = [
        r"\bcho\s+(.+?)(?:,\s*(?:s|sá|sá»|email|phone)|\.\s*(?:ship|email|phone)|$)",
        r"\bfor\s+(.+?)(?:,\s*(?:s|email|phone)|\.\s*(?:ship|email|phone)|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, query, flags=re.IGNORECASE)
        if match:
            name = match.group(1).strip(" ,.;:")
            name = re.sub(r"^(ch[ịi]\s+|anh\s+|bạn\s+)", "", name, flags=re.IGNORECASE).strip()
            if name and not re.search(r"\d|@", name):
                return name
    return ""


def _extract_shipping_address(query: str) -> str:
    patterns = [
        r"(?:giao\s*(?:hàng\s*)?(?:đến|tới|về)|giao\s*(?:hÃ ng\s*)?(?:Ä‘áº¿n|tá»›i|vá»)|ship to)\s+(.+?)(?:\.\s*(?:phone|email|ch|tôi|mình)|,\s*(?:số|sá»|email|phone)|\s+Phone\b|\s+email\b|$)",
        r"(?:địa chỉ giao hàng|Ä‘á»‹a chá»‰ giao hÃ ng)\s+(.+?)(?:\.\s*(?:phone|email|ch|tôi|mình)|,\s*(?:số|sá»|email|phone)|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, query, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip(" ,.;:")
    return ""


def _extract_items(query: str, store: OrderDataStore) -> list[OrderLineInput]:
    found: list[OrderLineInput] = []
    occupied: list[tuple[int, int]] = []

    products = sorted(store.products, key=lambda product: len(product.name), reverse=True)
    for product in products:
        match = re.search(re.escape(product.name), query, flags=re.IGNORECASE)
        if not match:
            continue
        if any(start <= match.start() < end or start < match.end() <= end for start, end in occupied):
            continue
        quantity = _quantity_before(query[: match.start()])
        found.append(OrderLineInput(product_id=product.product_id, quantity=quantity))
        occupied.append((match.start(), match.end()))

    return sorted(found, key=lambda item: item.product_id)


def _quantity_before(prefix: str) -> int:
    context = prefix[-100:]
    delimiter_positions = [context.rfind(delimiter) for delimiter in [",", ";", ":", '"', "'"]]
    for word_delimiter in [" và ", " va ", " vÃ", " and "]:
        delimiter_positions.append(context.lower().rfind(word_delimiter))
    last_delimiter = max(delimiter_positions)
    if last_delimiter >= 0:
        context = context[last_delimiter + 1 :]

    numbers = [int(item) for item in re.findall(r"\b([1-9]\d?)\b", context)]
    if not numbers:
        return 1
    candidate = numbers[-1]
    return candidate if 1 <= candidate <= 99 else 1
