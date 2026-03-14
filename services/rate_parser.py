import html
import re


CANONICAL_PREFIXES = {
    "gpt": "GPT",
    "claude": "Claude",
    "gemini": "Gemini",
}


def normalize_handle_case(handle):
    if not handle:
        return handle

    parts = re.split(r"([\-_.])", handle)
    if not parts:
        return handle

    prefix = parts[0].lower()
    canonical = CANONICAL_PREFIXES.get(prefix)
    if not canonical:
        return handle

    parts[0] = canonical
    return "".join(parts)


def extract_redirect_handle(location):
    if not location:
        return None

    match = re.search(r"https?://poe\.com/([^/?#]+)|^/([^/?#]+)", location, flags=re.IGNORECASE)
    if not match:
        return None

    handle = match.group(1) or match.group(2)
    return handle or None


def extract_bot_id(page_text):
    match = re.search(r'"botId":(\d+)', page_text)
    return int(match.group(1)) if match else None


def render_cache_discount_html(value):
    if not value:
        return "N/A"

    parts = []
    last_end = 0

    for match in re.finditer(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", value):
        start, end = match.span()
        if start > last_end:
            parts.append(html.escape(value[last_end:start]))

        text, url = match.groups()
        safe_text = html.escape(text)
        safe_url = html.escape(url, quote=True)
        parts.append(f'<a href="{safe_url}" target="_blank" rel="noopener noreferrer">{safe_text}</a>')
        last_end = end

    if last_end == 0:
        return html.escape(value)

    if last_end < len(value):
        parts.append(html.escape(value[last_end:]))

    return "".join(parts)


def parse_rate_markdown(markdown):
    rates = {
        "input_usd": "N/A",
        "input_points": "N/A",
        "output_usd": "N/A",
        "output_points": "N/A",
        "cache_discount": "N/A",
    }

    input_row = re.search(
        r"\|\s*(?:输入\s*[\(（]文本[\)）]|Input\s*\(text\)|输入|Input)\s*\|\s*(?P<price>.*?)\s*\|\s*(?P<points>.*?)\s*\|",
        markdown,
        flags=re.IGNORECASE,
    )
    if input_row:
        usd_bold = re.search(r"\*\*(\$[\d.]+)\*\*", input_row.group("price"))
        usd_plain = re.search(r"(\$[\d.]+)", input_row.group("price"))
        price = usd_bold.group(1) if usd_bold else (usd_plain.group(1) if usd_plain else None)
        rates["input_usd"] = f"{price}/百万词元" if price else "N/A"
        rates["input_points"] = input_row.group("points").strip()

    output_row = re.search(
        r"\|\s*(?:输出\s*[\(（]文本[\)）]|Output\s*\(text\))\s*\|\s*(?P<price>.*?)\s*\|\s*(?P<points>.*?)\s*\|",
        markdown,
        flags=re.IGNORECASE,
    )
    if output_row:
        usd_bold = re.search(r"\*\*(\$[\d.]+)\*\*", output_row.group("price"))
        usd_plain = re.search(r"(\$[\d.]+)", output_row.group("price"))
        price = usd_bold.group(1) if usd_bold else (usd_plain.group(1) if usd_plain else None)
        rates["output_usd"] = f"{price}/百万词元" if price else "N/A"
        rates["output_points"] = output_row.group("points").strip()

    cache_row = re.search(r"\|\s*(?:缓存折扣|Cache discount)\s*\|\s*(.*?)\s*\|", markdown, flags=re.IGNORECASE)
    if cache_row:
        rates["cache_discount"] = render_cache_discount_html(cache_row.group(1).strip())

    return {
        "input": {"usd": rates["input_usd"], "points": rates["input_points"]},
        "output": {"usd": rates["output_usd"], "points": rates["output_points"]},
        "cache_discount": rates["cache_discount"],
    }
