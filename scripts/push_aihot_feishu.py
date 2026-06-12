"""Fetch AIHOT news, rank it with OpenAI, and push the Top 5 to Feishu."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests
import yaml


ROOT_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT_DIR / "config.yaml"
BEIJING_TZ = timezone(timedelta(hours=8))


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def load_config() -> dict[str, Any]:
    """Load all business rules from config.yaml."""
    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        logger.error("Missing required environment variable: %s", name)
        sys.exit(1)
    return value


def request_json_with_retry(
    method: str,
    url: str,
    *,
    retries: int,
    timeout: int,
    **kwargs: Any,
) -> Any:
    """Call an HTTP JSON endpoint and retry transient failures."""
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = requests.request(method, url, timeout=timeout, **kwargs)
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # requests can raise several concrete errors here.
            last_error = exc
            logger.warning("HTTP request failed, attempt %s/%s: %s", attempt + 1, retries + 1, exc)
            if attempt < retries:
                time.sleep(2)
    raise RuntimeError(f"HTTP request failed after retries: {last_error}") from last_error


def extract_items(payload: Any) -> list[dict[str, Any]]:
    """Normalize likely AIHOT response shapes into a list of item dictionaries."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if not isinstance(payload, dict):
        return []

    for key in ("items", "data", "result", "list", "records"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = extract_items(value)
            if nested:
                return nested

    return []


def first_value(item: dict[str, Any], names: tuple[str, ...], default: str = "") -> str:
    for name in names:
        value = item.get(name)
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)
    return default


def item_id(item: dict[str, Any], index: int) -> str:
    value = first_value(item, ("id", "item_id", "uuid", "url", "link", "title"), default=f"item-{index}")
    return value.strip() or f"item-{index}"


def title_of(item: dict[str, Any]) -> str:
    return first_value(item, ("title", "name", "headline"))


def summary_of(item: dict[str, Any]) -> str:
    return first_value(item, ("summary", "description", "desc", "content", "text", "abstract"))


def category_of(item: dict[str, Any]) -> str:
    return first_value(item, ("category", "categories", "tag", "tags", "type"))


def source_of(item: dict[str, Any]) -> str:
    return first_value(item, ("source", "site", "platform", "author"))


def url_of(item: dict[str, Any]) -> str:
    return first_value(item, ("url", "link", "source_url", "original_url"))


def raw_score_of(item: dict[str, Any]) -> float:
    value = first_value(item, ("ai_hot_score", "score", "hot_score", "hot", "heat", "rank_score"), default="0")
    match = re.search(r"-?\d+(?:\.\d+)?", value)
    return float(match.group(0)) if match else 0.0


def clamp_score(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(100.0, number))


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def truncate_cn(value: str, limit: int = 100) -> str:
    text = clean_text(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def contains_keyword(text: str, keywords: list[str]) -> bool:
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in keywords if keyword)


def split_categories(category: str) -> list[str]:
    if not category:
        return []
    return [part.strip() for part in re.split(r"[,，/|、\s]+", category) if part.strip()]


def has_valid_category(category: str, valid_categories: list[str]) -> bool:
    parts = split_categories(category)
    if not parts:
        return False
    lowered_valid = [value.lower() for value in valid_categories]
    for part in parts:
        lowered_part = part.lower()
        if any(valid in lowered_part or lowered_part in valid for valid in lowered_valid):
            return True
    return False


def passes_initial_filter(item: dict[str, Any], filter_config: dict[str, Any]) -> bool:
    """Apply keep, exclude, and category filtering from config.yaml."""
    searchable_text = " ".join(
        [
            title_of(item),
            summary_of(item),
            category_of(item),
            source_of(item),
        ]
    )

    if contains_keyword(searchable_text, filter_config.get("exclude_keywords", [])):
        return False

    keyword_hit = contains_keyword(searchable_text, filter_config.get("keep_keywords", []))
    category_hit = has_valid_category(category_of(item), filter_config.get("valid_categories", []))
    return keyword_hit or category_hit


def fetch_aihot_items(config: dict[str, Any]) -> list[dict[str, Any]]:
    api_config = config["api"]
    payload = request_json_with_retry(
        "GET",
        api_config["ai_hot_url"],
        retries=int(api_config.get("request_retry", 0)),
        timeout=int(api_config.get("timeout_seconds", 30)),
        params={
            "mode": api_config["mode"],
            "take": api_config["default_limit"],
        },
    )
    return extract_items(payload)


def extract_response_text(payload: dict[str, Any]) -> str:
    """Read text from the OpenAI Responses API JSON shape."""
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]

    output = payload.get("output", [])
    texts: list[str] = []
    if isinstance(output, list):
        for message in output:
            if not isinstance(message, dict):
                continue
            content = message.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        text = block.get("text") or block.get("output_text")
                        if isinstance(text, str):
                            texts.append(text)
    return "\n".join(texts).strip()


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse model JSON, allowing for accidental Markdown fences."""
    stripped = text.strip()
    stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
    stripped = re.sub(r"\s*```$", "", stripped)

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, flags=re.S)
        if not match:
            raise
        return json.loads(match.group(0))


def call_openai(prompt: str, config: dict[str, Any], api_key: str) -> dict[str, Any]:
    openai_config = config["openai"]
    url = openai_config["host"].rstrip("/") + openai_config["api_path"]
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": openai_config["model"],
        "input": prompt,
    }
    payload = request_json_with_retry(
        "POST",
        url,
        retries=int(openai_config.get("retry", 0)),
        timeout=int(openai_config.get("timeout_seconds", 180)),
        headers=headers,
        json=body,
    )
    text = extract_response_text(payload)
    if not text:
        raise RuntimeError("OpenAI response did not contain output text")
    return parse_json_object(text)


def score_candidate(
    item: dict[str, Any],
    index: int,
    config: dict[str, Any],
    api_key: str,
) -> dict[str, Any]:
    openai_config = config["openai"]
    prompt = openai_config["scoring_prompt"].format(
        title=title_of(item),
        summary=summary_of(item),
        category=category_of(item),
        source=source_of(item),
        score=raw_score_of(item),
    )
    model_score = call_openai(prompt, config, api_key)

    weights = config["weights"]
    ai_hot_score = clamp_score(raw_score_of(item))
    relevance_score = clamp_score(model_score.get("relevance_score"))
    importance_score = clamp_score(model_score.get("importance_score"))
    practical_score = clamp_score(model_score.get("practical_score"))
    novelty_score = clamp_score(model_score.get("novelty_score"))

    # The configured weights sum to 1.0, so final_score remains on a 0-100 scale.
    final_score = (
        ai_hot_score * float(weights["ai_hot_score_weight"])
        + relevance_score * float(weights["relevance_weight"])
        + importance_score * float(weights["importance_weight"])
        + practical_score * float(weights["practical_weight"])
        + novelty_score * float(weights["novelty_weight"])
    )

    return {
        "id": item_id(item, index),
        "title": title_of(item),
        "summary": summary_of(item),
        "category": category_of(item),
        "source": source_of(item),
        "url": url_of(item),
        "ai_hot_score": ai_hot_score,
        "relevance_score": relevance_score,
        "importance_score": importance_score,
        "practical_score": practical_score,
        "novelty_score": novelty_score,
        "final_score": round(final_score, 2),
        "raw_item": item,
    }


def score_candidates(
    items: list[dict[str, Any]],
    config: dict[str, Any],
    api_key: str,
) -> list[dict[str, Any]]:
    scored: list[dict[str, Any]] = []
    interval = float(config["openai"].get("request_interval_seconds", 0))

    for index, item in enumerate(items, start=1):
        logger.info("Scoring candidate %s/%s: %s", index, len(items), title_of(item))
        scored.append(score_candidate(item, index, config, api_key))
        if interval > 0 and index < len(items):
            time.sleep(interval)

    filter_config = config["filter"]
    return [
        item
        for item in scored
        if item["relevance_score"] >= float(filter_config["relevance_threshold"])
        and item["final_score"] >= float(filter_config["final_score_threshold"])
    ]


def rank_candidates(
    candidates: list[dict[str, Any]],
    config: dict[str, Any],
    api_key: str,
) -> list[dict[str, Any]]:
    target_top_n = int(config["filter"]["target_top_n"])
    ranking_payload = [
        {
            "id": item["id"],
            "title": item["title"],
            "summary": item["summary"],
            "category": item["category"],
            "source": item["source"],
            "url": item["url"],
            "ai_hot_score": item["ai_hot_score"],
            "relevance_score": item["relevance_score"],
            "importance_score": item["importance_score"],
            "practical_score": item["practical_score"],
            "novelty_score": item["novelty_score"],
            "final_score": item["final_score"],
        }
        for item in candidates
    ]
    prompt = config["openai"]["ranking_prompt"].format(
        count=len(ranking_payload),
        top_n=target_top_n,
        items_json=json.dumps(ranking_payload, ensure_ascii=False),
    )
    ranking_result = call_openai(prompt, config, api_key)
    top_items = ranking_result.get("top_items", [])
    if not isinstance(top_items, list):
        raise RuntimeError("OpenAI ranking response does not contain top_items list")

    by_id = {item["id"]: item for item in candidates}
    ranked: list[dict[str, Any]] = []
    for entry in sorted(top_items, key=lambda value: int(value.get("rank", 999))):
        selected = by_id.get(str(entry.get("id")))
        if selected:
            ranked.append(selected)
        if len(ranked) >= target_top_n:
            break

    # If the model returns fewer valid IDs, fill the rest by final_score without inventing news.
    if len(ranked) < target_top_n:
        seen_ids = {item["id"] for item in ranked}
        for item in sorted(candidates, key=lambda value: value["final_score"], reverse=True):
            if item["id"] not in seen_ids:
                ranked.append(item)
            if len(ranked) >= target_top_n:
                break

    return ranked[:target_top_n]


def build_overall_summary(items: list[dict[str, Any]], config: dict[str, Any]) -> str:
    text = " ".join([item["title"] + " " + item["summary"] + " " + item["category"] for item in items])
    keyword_hits = [
        keyword
        for keyword in config["filter"].get("keep_keywords", [])
        if keyword and keyword.lower() in text.lower()
    ]
    category_hits = [
        category
        for category in config["filter"].get("valid_categories", [])
        if category and category.lower() in text.lower()
    ]
    themes = []
    for value in keyword_hits + category_hits:
        if value not in themes:
            themes.append(value)
        if len(themes) >= 4:
            break

    if themes:
        return truncate_cn(f"今日入选新闻聚焦{'、'.join(themes)}，整体围绕可落地的AI应用与平台动态。", 100)
    return truncate_cn("今日入选新闻聚焦AI行业动态，整体围绕可落地应用、效率提升与平台变化。", 100)


def build_feishu_message(items: list[dict[str, Any]], config: dict[str, Any]) -> str:
    now_text = datetime.now(BEIJING_TZ).strftime("%Y/%m/%d %H:%M")
    lines = [
        f"🤖 今日AI行业热点（{now_text}）",
        "",
        "✍️ 今日总结：",
        build_overall_summary(items, config),
        "",
        "---",
        "",
        "🧩 热点新闻TOP5",
        "",
    ]

    for index, item in enumerate(items, start=1):
        title = clean_text(item["title"]) or "未命名新闻"
        link = clean_text(item["url"]) or "无链接"
        summary = truncate_cn(item["summary"] or item["title"], 100)
        lines.extend(
            [
                f"{index}. [{title}]({link})",
                "",
                "新闻摘要：",
                summary,
                "",
                "---",
                "",
            ]
        )

    return "\n".join(lines).rstrip()


def push_to_feishu(message: str, webhook: str) -> None:
    response = requests.post(
        webhook,
        timeout=30,
        json={
            "msg_type": "text",
            "content": {
                "text": message,
            },
        },
    )
    response.raise_for_status()

    try:
        payload = response.json()
    except ValueError:
        return

    code = payload.get("code", payload.get("StatusCode", 0))
    if code not in (0, "0"):
        raise RuntimeError(f"Feishu webhook returned failure: {payload}")


def push_empty_message(feishu_webhook: str) -> None:
    push_to_feishu("今日暂无可用AI行业新闻数据。", feishu_webhook)


def main() -> None:
    config = load_config()
    openai_api_key = require_env("OPENAI_API_KEY")
    feishu_webhook = require_env("FEISHU_WEBHOOK")

    try:
        raw_items = fetch_aihot_items(config)
    except Exception:
        logger.exception("Failed to fetch AIHOT data")
        sys.exit(1)

    if not raw_items:
        logger.info("AIHOT API returned no items; pushing empty message.")
        try:
            push_empty_message(feishu_webhook)
        except Exception:
            logger.exception("Failed to push empty Feishu message")
            sys.exit(1)
        return

    filter_config = config["filter"]
    filtered = [item for item in raw_items if passes_initial_filter(item, filter_config)]
    filtered.sort(key=raw_score_of, reverse=True)
    ranking_pool = filtered[: int(filter_config["ranking_pool_size"])]

    if not ranking_pool:
        logger.info("No items passed initial filters; pushing empty message.")
        try:
            push_empty_message(feishu_webhook)
        except Exception:
            logger.exception("Failed to push empty Feishu message")
            sys.exit(1)
        return

    try:
        scored_candidates = score_candidates(ranking_pool, config, openai_api_key)
        if not scored_candidates:
            logger.info("No items passed score thresholds; pushing empty message.")
            push_empty_message(feishu_webhook)
            return

        top_items = rank_candidates(scored_candidates, config, openai_api_key)
    except Exception:
        logger.exception("OpenAI scoring or ranking failed")
        sys.exit(1)

    message = build_feishu_message(top_items, config)

    try:
        push_to_feishu(message, feishu_webhook)
    except Exception:
        logger.exception("Failed to push message to Feishu")
        sys.exit(1)

    logger.info("Successfully pushed %s AIHOT items to Feishu.", len(top_items))


if __name__ == "__main__":
    main()
