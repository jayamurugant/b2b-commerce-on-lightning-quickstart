#!/usr/bin/env python3
"""Console stock monitor for FirstCry brand listing APIs.

Features:
- Polls listing API every random interval (default 30-45 seconds)
- Prints summary to console only (no file writes)
- Tracks previous stock in memory
- Sends email alert when any product's stock increases versus prior poll
- Supports watched product IDs (comma-separated) and alerts when they come in stock
"""

import argparse
import datetime as dt
import html
import json
import os
import random
import re
import smtplib
import sys
import time
import urllib.parse
import urllib.request
from email.message import EmailMessage
from typing import Dict, List, Optional, Tuple


BASE_API = "https://www.firstcry.com/svcs/SearchResult.svc"

ANSI_RESET = "\033[0m"
ANSI_GREEN = "\033[92m"
ANSI_CYAN = "\033[96m"
ANSI_YELLOW = "\033[93m"


def parse_product_ids(value: str) -> List[str]:
    """Parse comma-separated product IDs, preserving order and uniqueness."""
    seen: Dict[str, bool] = {}
    items: List[str] = []
    for raw in value.split(","):
        pid = raw.strip()
        if not pid or pid in seen:
            continue
        seen[pid] = True
        items.append(pid)
    return items


def parse_csv_values(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_int_csv_values(value: str, option_name: str) -> List[int]:
    seen: Dict[int, bool] = {}
    items: List[int] = []
    for raw in value.split(","):
        token = raw.strip()
        if not token:
            continue
        try:
            parsed = int(token)
        except ValueError as exc:
            raise ValueError(f"{option_name} contains non-integer value: {token}") from exc
        if parsed in seen:
            continue
        seen[parsed] = True
        items.append(parsed)
    return items


def _enable_windows_ansi() -> None:
    # Enable ANSI color support on modern Windows terminals.
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        # Fall back to plain text if ANSI setup fails.
        pass


def init_console_color_support(args: argparse.Namespace) -> None:
    if getattr(args, "no_color", False):
        args._color_enabled = False
        return
    _enable_windows_ansi()
    args._color_enabled = bool(getattr(sys.stdout, "isatty", lambda: False)())


def colorize(args: argparse.Namespace, text: str, color_code: str) -> str:
    if getattr(args, "_color_enabled", False):
        return f"{color_code}{text}{ANSI_RESET}"
    return text


def load_proxy_file(path: str) -> List[str]:
    proxies: List[str] = []
    with open(path, "r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            proxies.append(line)
    return proxies


class ProxyRotator:
    """Rotates proxy URLs per request with optional provider refresh."""

    def __init__(
        self,
        static_proxies: List[str],
        provider_url: str,
        provider_token: str,
        refresh_seconds: int,
        strategy: str,
        request_timeout: int,
        print_proxy: bool,
    ) -> None:
        self.provider_url = provider_url.strip()
        self.provider_token = provider_token.strip()
        self.refresh_seconds = max(10, refresh_seconds)
        self.strategy = strategy
        self.request_timeout = max(5, request_timeout)
        self.print_proxy = print_proxy
        self._pool: List[str] = self._dedupe(static_proxies)
        self._index = 0
        self._last_proxy = ""
        self._last_refresh = 0.0

    @staticmethod
    def _normalize_proxy(value: str) -> str:
        proxy = value.strip()
        if not proxy:
            return ""
        parsed = urllib.parse.urlparse(proxy)
        if not parsed.scheme:
            # Assume HTTP proxy if no scheme provided.
            proxy = "http://" + proxy
            parsed = urllib.parse.urlparse(proxy)
        if not parsed.netloc:
            return ""
        return proxy

    def _dedupe(self, values: List[str]) -> List[str]:
        seen: Dict[str, bool] = {}
        items: List[str] = []
        for raw in values:
            proxy = self._normalize_proxy(raw)
            if not proxy or proxy in seen:
                continue
            seen[proxy] = True
            items.append(proxy)
        return items

    @property
    def enabled(self) -> bool:
        return bool(self._pool or self.provider_url)

    @property
    def pool_size(self) -> int:
        return len(self._pool)

    def _parse_provider_payload(self, payload: str) -> List[str]:
        payload = payload.strip()
        if not payload:
            return []

        try:
            data = json.loads(payload)
        except Exception:
            data = None

        if isinstance(data, list):
            return [str(item).strip() for item in data if str(item).strip()]
        if isinstance(data, dict):
            if isinstance(data.get("proxies"), list):
                return [str(item).strip() for item in data["proxies"] if str(item).strip()]
            if isinstance(data.get("data"), list):
                return [str(item).strip() for item in data["data"] if str(item).strip()]
            if isinstance(data.get("proxy"), str) and data["proxy"].strip():
                return [data["proxy"].strip()]

        # Plain-text fallback: comma/newline separated.
        values: List[str] = []
        for chunk in re.split(r"[\n,]+", payload):
            item = chunk.strip()
            if item:
                values.append(item)
        return values

    def _fetch_provider_proxies(self) -> List[str]:
        if not self.provider_url:
            return []
        headers = {"User-Agent": "firstcry-stock-monitor/1.0"}
        if self.provider_token:
            headers["Authorization"] = f"Bearer {self.provider_token}"
        request = urllib.request.Request(self.provider_url, headers=headers)
        with urllib.request.urlopen(request, timeout=self.request_timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
        return self._parse_provider_payload(body)

    def _refresh_provider_if_needed(self, force: bool = False) -> None:
        if not self.provider_url:
            return
        now = time.time()
        should_refresh = force or not self._pool or (now - self._last_refresh >= self.refresh_seconds)
        if not should_refresh:
            return
        provider_proxies = self._fetch_provider_proxies()
        merged = self._dedupe(self._pool + provider_proxies)
        self._pool = merged
        self._last_refresh = now
        if self._index >= len(self._pool):
            self._index = 0

    def prime(self) -> None:
        self._refresh_provider_if_needed(force=True)

    def describe(self) -> str:
        provider_mode = "enabled" if self.provider_url else "disabled"
        return (
            f"Proxy rotation ON | pool={self.pool_size} | strategy={self.strategy} | "
            f"provider={provider_mode}"
        )

    def _pick_round_robin(self) -> str:
        candidate = self._pool[self._index % len(self._pool)]
        self._index = (self._index + 1) % len(self._pool)
        if len(self._pool) > 1 and candidate == self._last_proxy:
            candidate = self._pool[self._index % len(self._pool)]
            self._index = (self._index + 1) % len(self._pool)
        return candidate

    def _pick_random(self) -> str:
        candidate = random.choice(self._pool)
        if len(self._pool) > 1 and candidate == self._last_proxy:
            alternatives = [item for item in self._pool if item != self._last_proxy]
            candidate = random.choice(alternatives)
        return candidate

    def next_proxy(self) -> Optional[str]:
        self._refresh_provider_if_needed(force=False)
        if not self._pool:
            return None
        if self.strategy == "random":
            proxy = self._pick_random()
        else:
            proxy = self._pick_round_robin()
        self._last_proxy = proxy
        return proxy


def attach_proxy_rotator(args: argparse.Namespace) -> None:
    static_proxies: List[str] = []
    if args.proxy_urls:
        static_proxies.extend(parse_csv_values(args.proxy_urls))
    if args.proxy_file:
        static_proxies.extend(load_proxy_file(args.proxy_file))

    rotator = ProxyRotator(
        static_proxies=static_proxies,
        provider_url=args.proxy_provider_url,
        provider_token=args.proxy_provider_token,
        refresh_seconds=args.proxy_refresh_seconds,
        strategy=args.proxy_strategy,
        request_timeout=args.proxy_timeout,
        print_proxy=args.print_proxy,
    )
    if rotator.enabled:
        rotator.prime()
    if args.strict_proxy_rotation and rotator.enabled and rotator.pool_size < 2:
        raise ValueError(
            "Strict proxy rotation requires at least 2 working proxies "
            "(from --proxy-urls, --proxy-file, or --proxy-provider-url)."
        )
    args._proxy_rotator = rotator


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor FirstCry stock changes and email on stock increases."
    )

    # Listing params for URL pattern like /hotwheels/0/0/113
    parser.add_argument("--brand-id", type=int, default=113, help="MasterBrand value")
    parser.add_argument("--onsale", type=int, default=0, help="OnSale value")
    parser.add_argument(
        "--onsale-values",
        default="",
        help="Comma-separated OnSale values (e.g. 0,5) to monitor in one run",
    )
    parser.add_argument(
        "--search-string", default="brand", help="SearchString API value"
    )
    parser.add_argument("--sort", default="Popularity", help="SortExpression")
    parser.add_argument("--page-size", type=int, default=20, help="PageSize")
    parser.add_argument("--pincode", default="0", help="pcode value")
    parser.add_argument(
        "--product-ids",
        default="",
        help="Comma-separated Product IDs to explicitly track availability",
    )
    parser.add_argument(
        "--new-product-min-stock",
        type=int,
        default=1,
        help="Minimum stock required to trigger new-product alerts",
    )
    parser.add_argument(
        "--exclude-out-of-stock",
        action="store_true",
        help="Send OutOfStock=0 in API request",
    )
    parser.add_argument(
        "--referer",
        default="https://www.firstcry.com/hotwheels/0/0/113",
        help="Referer header",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=100,
        help="Safety limit while paging full catalog",
    )

    # Polling loop
    parser.add_argument(
        "--min-interval",
        type=int,
        default=30,
        help="Minimum random sleep seconds",
    )
    parser.add_argument(
        "--max-interval",
        type=int,
        default=45,
        help="Maximum random sleep seconds",
    )
    parser.add_argument(
        "--max-runs",
        type=int,
        default=0,
        help="0 means run forever; otherwise stop after N runs",
    )

    # Proxy / IP rotation
    parser.add_argument(
        "--proxy-urls",
        default="",
        help="Comma-separated proxy URLs, e.g. http://user:pass@host:port",
    )
    parser.add_argument(
        "--proxy-file",
        default="",
        help="Path to proxy list file (one proxy URL per line)",
    )
    parser.add_argument(
        "--proxy-provider-url",
        default="",
        help="Endpoint returning proxy list (JSON or plain text)",
    )
    parser.add_argument(
        "--proxy-provider-token",
        default="",
        help="Bearer token for proxy provider endpoint",
    )
    parser.add_argument(
        "--proxy-refresh-seconds",
        type=int,
        default=300,
        help="How often to refresh proxies from provider endpoint",
    )
    parser.add_argument(
        "--proxy-strategy",
        choices=["round_robin", "random"],
        default="round_robin",
        help="Rotation strategy across proxies",
    )
    parser.add_argument(
        "--proxy-timeout",
        type=int,
        default=20,
        help="HTTP timeout in seconds for proxy provider request",
    )
    parser.add_argument(
        "--strict-proxy-rotation",
        action="store_true",
        help="Fail startup if fewer than 2 proxies are available",
    )
    parser.add_argument(
        "--print-proxy",
        action="store_true",
        help="Print proxy used for each API request",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored console output",
    )

    # Email config
    parser.add_argument("--smtp-host", default="", help="SMTP host")
    parser.add_argument("--smtp-port", type=int, default=587, help="SMTP port")
    parser.add_argument("--smtp-user", default="", help="SMTP username")
    parser.add_argument("--smtp-password", default="", help="SMTP password")
    parser.add_argument(
        "--smtp-no-starttls",
        action="store_true",
        help="Disable STARTTLS",
    )
    parser.add_argument(
        "--smtp-ssl",
        action="store_true",
        help="Use SMTP SSL connection",
    )
    parser.add_argument("--email-from", default="", help="Sender email")
    parser.add_argument(
        "--email-to",
        default="",
        help="Comma-separated recipient emails",
    )
    parser.add_argument(
        "--email-subject-prefix",
        default="[FirstCry Stock Alert]",
        help="Prefix for email subject",
    )

    args = parser.parse_args()
    if args.min_interval > args.max_interval:
        parser.error("--min-interval cannot be greater than --max-interval")
    if args.new_product_min_stock < 0:
        parser.error("--new-product-min-stock cannot be negative")
    if args.onsale_values.strip():
        try:
            args.onsale_list = parse_int_csv_values(args.onsale_values, "--onsale-values")
        except ValueError as exc:
            parser.error(str(exc))
    else:
        args.onsale_list = [args.onsale]
    if not args.onsale_list:
        parser.error("No valid OnSale values provided")
    # Keep existing single-value flow compatibility with current helpers.
    args.onsale = args.onsale_list[0]
    args.watch_product_ids = parse_product_ids(args.product_ids)
    try:
        attach_proxy_rotator(args)
    except Exception as exc:
        parser.error(str(exc))
    init_console_color_support(args)
    return args


def to_int(value: object, default: int = 0) -> int:
    try:
        if value is None:
            return default
        if isinstance(value, str):
            value = value.strip()
            if value == "":
                return default
        return int(value)
    except Exception:
        return default


def parse_count(raw_count: object) -> Optional[int]:
    # API frequently returns Count as list e.g. [241]
    if isinstance(raw_count, list) and raw_count:
        return to_int(raw_count[0], default=0)
    if raw_count is None:
        return None
    return to_int(raw_count, default=0)


def slugify_for_url(value: str) -> str:
    token = value.strip().lower()
    token = token.replace("&", " and ")
    token = re.sub(r"[^a-z0-9]+", "-", token)
    token = re.sub(r"-+", "-", token).strip("-")
    return token or "product"


def build_product_search_url(product_id: str) -> str:
    return f"https://www.firstcry.com/search?query={urllib.parse.quote(product_id)}"


def build_product_detail_url(product_id: str, name: str, brand_name: str) -> str:
    # Best-effort URL shape used by FirstCry product detail pages.
    # If route format changes, search URL remains a reliable fallback.
    brand_slug = slugify_for_url(brand_name or "product")
    name_slug = slugify_for_url(name or product_id)
    return (
        f"https://www.firstcry.com/{brand_slug}/{name_slug}/"
        f"{urllib.parse.quote(product_id)}/product-detail"
    )


def build_params(
    args: argparse.Namespace, page_no: int, override_exclude: Optional[bool] = None
) -> Dict[str, str]:
    params = {
        "PageNo": str(page_no),
        "PageSize": str(args.page_size),
        "SortExpression": args.sort,
        "OnSale": str(args.onsale),
        "SearchString": args.search_string,
        "MasterBrand": str(args.brand_id),
        "pcode": str(args.pincode),
        "isclub": "0",
    }
    exclude_out_of_stock = args.exclude_out_of_stock
    if override_exclude is not None:
        exclude_out_of_stock = override_exclude
    if exclude_out_of_stock:
        params["OutOfStock"] = "0"
    return params


def endpoint_for_page(page_no: int) -> str:
    if page_no == 1:
        return "GetSearchResultProductsFilters"
    return "GetSearchResultProductsPaging"


def request_listing(
    args: argparse.Namespace, page_no: int, override_exclude: Optional[bool] = None
) -> Dict[str, object]:
    endpoint = endpoint_for_page(page_no)
    params = build_params(args, page_no, override_exclude=override_exclude)
    url = f"{BASE_API}/{endpoint}?{urllib.parse.urlencode(params)}"

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": args.referer,
    }

    req = urllib.request.Request(url, headers=headers)
    proxy_url: Optional[str] = None
    rotator: Optional[ProxyRotator] = getattr(args, "_proxy_rotator", None)
    if rotator and rotator.enabled:
        proxy_url = rotator.next_proxy()
        if proxy_url and rotator.print_proxy:
            print(f"Using proxy: {proxy_url}")

    if proxy_url:
        proxy_handler = urllib.request.ProxyHandler(
            {"http": proxy_url, "https": proxy_url}
        )
        opener = urllib.request.build_opener(proxy_handler)
        with opener.open(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    else:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")

    outer = json.loads(body)
    raw_inner = outer.get("ProductResponse", "{}")
    inner = json.loads(raw_inner) if isinstance(raw_inner, str) else raw_inner
    if not isinstance(inner, dict):
        raise ValueError("Unexpected API response shape for ProductResponse")
    return inner


def fetch_all_products(
    args: argparse.Namespace,
    override_exclude: Optional[bool] = None,
) -> Tuple[List[Dict[str, object]], Optional[int], int]:
    all_products: List[Dict[str, object]] = []
    total_count: Optional[int] = None
    pages_fetched = 0

    for page_no in range(1, args.max_pages + 1):
        inner = request_listing(args, page_no, override_exclude=override_exclude)
        pages_fetched += 1
        products = inner.get("Products", [])
        if not isinstance(products, list):
            raise ValueError("Unexpected Products type in API response")

        if page_no == 1:
            total_count = parse_count(inner.get("Count"))

        if not products:
            break

        all_products.extend(products)

        if len(products) < args.page_size:
            break
        if total_count is not None and len(all_products) >= total_count:
            break

    return all_products, total_count, pages_fetched


def build_stock_map(products: List[Dict[str, object]]) -> Dict[str, int]:
    stock_map: Dict[str, int] = {}
    for item in products:
        pid = str(item.get("PId", "")).strip()
        if not pid:
            continue
        stock_map[pid] = to_int(item.get("CrntStock"), default=0)
    return stock_map


def detect_increases(
    previous: Dict[str, int], current: Dict[str, int], products: List[Dict[str, object]]
) -> List[Dict[str, object]]:
    by_id: Dict[str, Dict[str, object]] = {}
    for item in products:
        pid = str(item.get("PId", "")).strip()
        if pid:
            by_id[pid] = item

    increases: List[Dict[str, object]] = []
    for pid, current_stock in current.items():
        if pid not in previous:
            continue
        prev_stock = previous[pid]
        if current_stock > prev_stock:
            item = by_id.get(pid, {})
            name = str(item.get("PNm", ""))
            brand_name = str(item.get("BNm", ""))
            detail_url = build_product_detail_url(pid, name, brand_name)
            search_url = build_product_search_url(pid)
            increases.append(
                {
                    "product_id": pid,
                    "name": name,
                    "previous_stock": prev_stock,
                    "current_stock": current_stock,
                    "delta": current_stock - prev_stock,
                    "detail_url": detail_url,
                    "search_url": search_url,
                }
            )
    return increases


def detect_new_listed_products(
    previous: Dict[str, int],
    current: Dict[str, int],
    products: List[Dict[str, object]],
    min_stock: int = 1,
) -> List[Dict[str, object]]:
    """Detect products that appeared in the listing for the first time."""
    by_id: Dict[str, Dict[str, object]] = {}
    for item in products:
        pid = str(item.get("PId", "")).strip()
        if pid:
            by_id[pid] = item

    events: List[Dict[str, object]] = []
    for pid, current_stock in current.items():
        if pid in previous:
            continue
        if current_stock < min_stock:
            continue
        item = by_id.get(pid, {})
        name = str(item.get("PNm", ""))
        brand_name = str(item.get("BNm", ""))
        detail_url = build_product_detail_url(pid, name, brand_name)
        search_url = build_product_search_url(pid)
        events.append(
            {
                "product_id": pid,
                "name": name,
                "current_stock": current_stock,
                "detail_url": detail_url,
                "search_url": search_url,
            }
        )
    return events


def count_in_stock_products(stock_map: Dict[str, int]) -> int:
    return sum(1 for qty in stock_map.values() if qty > 0)


def detect_in_stock_count_increase(
    previous_count: Optional[int], current_count: int
) -> Optional[Dict[str, int]]:
    if previous_count is None:
        return None
    if current_count > previous_count:
        return {
            "previous_count": previous_count,
            "current_count": current_count,
            "delta": current_count - previous_count,
        }
    return None


def detect_in_stock_count_products(
    previous: Dict[str, int], current: Dict[str, int], products: List[Dict[str, object]]
) -> List[Dict[str, object]]:
    """Products that moved from out-of-stock/non-present to in-stock."""
    by_id: Dict[str, Dict[str, object]] = {}
    for item in products:
        pid = str(item.get("PId", "")).strip()
        if pid:
            by_id[pid] = item

    events: List[Dict[str, object]] = []
    for pid, current_stock in current.items():
        previous_stock = previous.get(pid, 0)
        if previous_stock <= 0 and current_stock > 0:
            item = by_id.get(pid, {})
            name = str(item.get("PNm", ""))
            brand_name = str(item.get("BNm", ""))
            detail_url = build_product_detail_url(pid, name, brand_name)
            search_url = build_product_search_url(pid)
            events.append(
                {
                    "product_id": pid,
                    "name": name,
                    "previous_stock": previous_stock,
                    "current_stock": current_stock,
                    "delta": current_stock - previous_stock,
                    "detail_url": detail_url,
                    "search_url": search_url,
                }
            )
    return events


def fetch_watched_products(
    args: argparse.Namespace, watch_product_ids: List[str]
) -> Tuple[Dict[str, Dict[str, object]], int]:
    """Fetch watched products with filter-aware lookup and fallback mode.

    Primary pass uses the current run filter context (including OutOfStock mode).
    If unresolved IDs remain, fallback pass uses the opposite OutOfStock mode to
    handle backend inconsistencies where a product appears only in one variant.
    """
    watch_set = set(watch_product_ids)
    found: Dict[str, Dict[str, object]] = {}
    pages_fetched = 0

    if not watch_set:
        return found, pages_fetched

    # None means "use current args.exclude_out_of_stock" behavior.
    mode_sequence: List[Optional[bool]] = [None]
    opposite_mode = not args.exclude_out_of_stock
    mode_sequence.append(opposite_mode)

    for override_mode in mode_sequence:
        unresolved = watch_set.difference(found.keys())
        if not unresolved:
            break

        for page_no in range(1, args.max_pages + 1):
            inner = request_listing(args, page_no, override_exclude=override_mode)
            pages_fetched += 1
            products = inner.get("Products", [])
            if not isinstance(products, list):
                raise ValueError("Unexpected Products type in watched product lookup")

            if not products:
                break

            for item in products:
                pid = str(item.get("PId", "")).strip()
                if pid in unresolved and pid not in found:
                    found[pid] = item

            unresolved = watch_set.difference(found.keys())
            if not unresolved:
                break
            if len(products) < args.page_size:
                break

    return found, pages_fetched


def build_watched_rows(
    watch_product_ids: List[str], watched_products: Dict[str, Dict[str, object]]
) -> Tuple[List[Dict[str, object]], Dict[str, int]]:
    rows: List[Dict[str, object]] = []
    watch_stock_map: Dict[str, int] = {}

    for pid in watch_product_ids:
        item = watched_products.get(pid)
        if not item:
            rows.append(
                {
                    "product_id": pid,
                    "name": "",
                    "stock": 0,
                    "status": "NOT_FOUND",
                }
            )
            watch_stock_map[pid] = 0
            continue

        stock = to_int(item.get("CrntStock"), default=0)
        status = "IN_STOCK" if stock > 0 else "OUT_OF_STOCK"
        rows.append(
            {
                "product_id": pid,
                "name": str(item.get("PNm", "")),
                "stock": stock,
                "status": status,
            }
        )
        watch_stock_map[pid] = stock

    return rows, watch_stock_map


def detect_watched_in_stock(
    previous_watch_stock: Dict[str, int],
    current_watch_stock: Dict[str, int],
    watched_products: Dict[str, Dict[str, object]],
    watch_product_ids: List[str],
) -> List[Dict[str, object]]:
    """Detect watched products that moved from no stock to in stock."""
    events: List[Dict[str, object]] = []
    for pid in watch_product_ids:
        if pid not in previous_watch_stock:
            continue
        previous_value = previous_watch_stock.get(pid, 0)
        current_value = current_watch_stock.get(pid, 0)
        if previous_value <= 0 and current_value > 0:
            item = watched_products.get(pid, {})
            name = str(item.get("PNm", ""))
            brand_name = str(item.get("BNm", ""))
            detail_url = build_product_detail_url(pid, name, brand_name)
            search_url = build_product_search_url(pid)
            events.append(
                {
                    "product_id": pid,
                    "name": name,
                    "previous_stock": previous_value,
                    "current_stock": current_value,
                    "delta": current_value - previous_value,
                    "detail_url": detail_url,
                    "search_url": search_url,
                }
            )
    return events


def parse_recipients(value: str) -> List[str]:
    return [addr.strip() for addr in value.split(",") if addr.strip()]


def can_send_email(args: argparse.Namespace) -> bool:
    recipients = parse_recipients(args.email_to)
    return bool(args.smtp_host and args.email_from and recipients)


def send_email_alert(
    args: argparse.Namespace,
    increases: List[Dict[str, object]],
    new_listed_products: List[Dict[str, object]],
    watched_in_stock_events: List[Dict[str, object]],
    in_stock_count_increase: Optional[Dict[str, int]],
    in_stock_count_products: List[Dict[str, object]],
    run_no: int,
    total_products: int,
    total_count: Optional[int],
) -> None:
    recipients = parse_recipients(args.email_to)
    onsale_context = getattr(args, "onsale", "NA")
    in_stock_count_alerts = 1 if in_stock_count_increase else 0
    total_alerts = (
        len(increases)
        + len(new_listed_products)
        + len(watched_in_stock_events)
        + in_stock_count_alerts
    )
    subject = (
        f"{args.email_subject_prefix} "
        f"[OnSale={onsale_context}] "
        f"{total_alerts} alert(s): {len(increases)} increase(s), "
        f"{len(new_listed_products)} new listed, "
        f"{len(watched_in_stock_events)} watched in-stock, "
        f"{in_stock_count_alerts} in-stock-count increase"
    )

    lines = [
        "FirstCry stock monitor alert.",
        f"Run: {run_no}",
        f"OnSale context: {onsale_context}",
        f"Products fetched: {total_products}",
        f"Catalog count: {total_count if total_count is not None else 'NA'}",
        f"Pincode context: {args.pincode}",
    ]

    if increases:
        lines.extend(["", "Products with stock increase:"])
        for row in increases:
            lines.append(
                "- {name} (PId {product_id}): {previous_stock} -> {current_stock} (+{delta})".format(
                    **row
                )
            )
            lines.append(
                "  Detail: {detail_url} | Search: {search_url}".format(**row)
            )
    if new_listed_products:
        lines.extend(["", "Newly listed products detected:"])
        for row in new_listed_products:
            lines.append(
                "- {name} (PId {product_id}) stock={current_stock}".format(**row)
            )
            lines.append(
                "  Detail: {detail_url} | Search: {search_url}".format(**row)
            )
    if watched_in_stock_events:
        lines.extend(["", "Watched products now in stock:"])
        for row in watched_in_stock_events:
            lines.append(
                "- {name} (PId {product_id}): {previous_stock} -> {current_stock} (+{delta})".format(
                    **row
                )
            )
            lines.append(
                "  Detail: {detail_url} | Search: {search_url}".format(**row)
            )
    if in_stock_count_increase:
        lines.extend(
            [
                "",
                "Overall in-stock product count increased:",
                "- {previous_count} -> {current_count} (+{delta})".format(
                    **in_stock_count_increase
                ),
            ]
        )
        if in_stock_count_products:
            lines.append("Products that moved into stock:")
            for row in in_stock_count_products:
                lines.append(
                    "- {name} (PId {product_id}): {previous_stock} -> {current_stock} (+{delta})".format(
                        **row
                    )
                )
                lines.append(
                    "  Detail: {detail_url} | Search: {search_url}".format(**row)
                )
        else:
            lines.append("Products that moved into stock: unable to resolve in this poll.")

    message = EmailMessage()
    message["From"] = args.email_from
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message.set_content("\n".join(lines))

    html_parts = [
        "<html><body>",
        "<h3>FirstCry stock monitor alert</h3>",
        f"<p><b>Run:</b> {run_no}<br>",
        f"<b>OnSale context:</b> {html.escape(str(onsale_context))}<br>",
        f"<b>Products fetched:</b> {total_products}<br>",
        f"<b>Catalog count:</b> {total_count if total_count is not None else 'NA'}<br>",
        f"<b>Pincode context:</b> {html.escape(str(args.pincode))}</p>",
    ]

    if increases:
        html_parts.append("<h4>Products with stock increase</h4><ul>")
        for row in increases:
            name = html.escape(str(row["name"]))
            detail_url = html.escape(str(row["detail_url"]))
            search_url = html.escape(str(row["search_url"]))
            html_parts.append(
                "<li>"
                f"{name} (PId {row['product_id']}): "
                f"{row['previous_stock']} &rarr; {row['current_stock']} (+{row['delta']})"
                f" | <a href=\"{detail_url}\">Open product</a>"
                f" | <a href=\"{search_url}\">Search by product ID</a>"
                "</li>"
            )
        html_parts.append("</ul>")

    if new_listed_products:
        html_parts.append("<h4>Newly listed products detected</h4><ul>")
        for row in new_listed_products:
            name = html.escape(str(row["name"]))
            detail_url = html.escape(str(row["detail_url"]))
            search_url = html.escape(str(row["search_url"]))
            html_parts.append(
                "<li>"
                f"{name} (PId {row['product_id']}): "
                f"stock={row['current_stock']}"
                f" | <a href=\"{detail_url}\">Open product</a>"
                f" | <a href=\"{search_url}\">Search by product ID</a>"
                "</li>"
            )
        html_parts.append("</ul>")

    if watched_in_stock_events:
        html_parts.append("<h4>Watched products now in stock</h4><ul>")
        for row in watched_in_stock_events:
            name = html.escape(str(row["name"]))
            detail_url = html.escape(str(row["detail_url"]))
            search_url = html.escape(str(row["search_url"]))
            html_parts.append(
                "<li>"
                f"{name} (PId {row['product_id']}): "
                f"{row['previous_stock']} &rarr; {row['current_stock']} (+{row['delta']})"
                f" | <a href=\"{detail_url}\">Open product</a>"
                f" | <a href=\"{search_url}\">Search by product ID</a>"
                "</li>"
            )
        html_parts.append("</ul>")

    if in_stock_count_increase:
        html_parts.append("<h4>Overall in-stock product count increased</h4>")
        html_parts.append(
            "<p>"
            "{previous_count} &rarr; {current_count} (+{delta})".format(
                **in_stock_count_increase
            )
            + "</p>"
        )
        if in_stock_count_products:
            html_parts.append("<p><b>Products that moved into stock:</b></p><ul>")
            for row in in_stock_count_products:
                name = html.escape(str(row["name"]))
                detail_url = html.escape(str(row["detail_url"]))
                search_url = html.escape(str(row["search_url"]))
                html_parts.append(
                    "<li>"
                    f"{name} (PId {row['product_id']}): "
                    f"{row['previous_stock']} &rarr; {row['current_stock']} (+{row['delta']})"
                    f" | <a href=\"{detail_url}\">Open product</a>"
                    f" | <a href=\"{search_url}\">Search by product ID</a>"
                    "</li>"
                )
            html_parts.append("</ul>")
        else:
            html_parts.append("<p>Products that moved into stock: unable to resolve in this poll.</p>")

    html_parts.append("</body></html>")
    message.add_alternative("\n".join(html_parts), subtype="html")

    if args.smtp_ssl:
        with smtplib.SMTP_SSL(args.smtp_host, args.smtp_port, timeout=30) as server:
            if args.smtp_user:
                server.login(args.smtp_user, args.smtp_password)
            server.send_message(message)
        return

    with smtplib.SMTP(args.smtp_host, args.smtp_port, timeout=30) as server:
        if not args.smtp_no_starttls:
            server.starttls()
        if args.smtp_user:
            server.login(args.smtp_user, args.smtp_password)
        server.send_message(message)


def print_run_header(run_no: int, args: argparse.Namespace) -> None:
    ts = dt.datetime.now().isoformat(timespec="seconds")
    print("=" * 80)
    print(f"[{ts}] Poll run #{run_no}")
    onsale_list = getattr(args, "onsale_list", [getattr(args, "onsale", "NA")])
    onsale_display = ",".join(str(value) for value in onsale_list)
    print(
        "Config: OnSale={onsale}, Brand={brand}, Pincode={pincode}, "
        "ExcludeOutOfStock={exclude}".format(
            onsale=onsale_display,
            brand=args.brand_id,
            pincode=args.pincode,
            exclude="ON" if args.exclude_out_of_stock else "OFF",
        )
    )
    if args.watch_product_ids:
        print(f"Watched Product IDs: {', '.join(args.watch_product_ids)}")
    rotator: Optional[ProxyRotator] = getattr(args, "_proxy_rotator", None)
    if rotator and rotator.enabled:
        print(rotator.describe())
    else:
        print("Proxy rotation: OFF")


def print_run_summary(
    args: argparse.Namespace,
    products: List[Dict[str, object]],
    total_count: Optional[int],
    pages_fetched: int,
    current_in_stock_count: int,
    in_stock_count_increase: Optional[Dict[str, int]],
    in_stock_count_products: List[Dict[str, object]],
    increases: List[Dict[str, object]],
    new_listed_products: List[Dict[str, object]],
    watched_rows: List[Dict[str, object]],
    watched_lookup_pages: int,
    watched_in_stock_events: List[Dict[str, object]],
) -> None:
    print(f"Pages fetched: {pages_fetched}")
    print(
        f"Products fetched: {len(products)} | Reported total count: "
        f"{total_count if total_count is not None else 'NA'}"
    )
    print(f"In-stock products in fetched set: {current_in_stock_count}")

    preview = products[:5]
    if preview:
        print("Top products:")
        for item in preview:
            pid = item.get("PId")
            name = item.get("PNm")
            stock = to_int(item.get("CrntStock"), default=0)
            print(f"- {name} (PId {pid}) stock={stock}")

    if watched_rows:
        print(
            "Watched product checks (filter-aware with fallback): "
            f"pages={watched_lookup_pages}"
        )
        for row in watched_rows:
            pid = row["product_id"]
            status = row["status"]
            stock = row["stock"]
            name = row["name"]
            if status == "IN_STOCK":
                print(f"- PId {pid}: IN STOCK (stock={stock}) | {name}")
            elif status == "OUT_OF_STOCK":
                print(f"- PId {pid}: OUT OF STOCK (stock={stock}) | {name}")
            else:
                print(f"- PId {pid}: NOT FOUND in current listing response")

    if increases:
        print(
            colorize(
                args,
                f"Stock increase check: {len(increases)} product(s) increased.",
                ANSI_GREEN,
            )
        )
        for row in increases:
            print(
                colorize(
                    args,
                    "- {name} (PId {product_id}) {previous_stock} -> {current_stock} (+{delta})".format(
                        **row
                    ),
                    ANSI_GREEN,
                )
            )
    else:
        print("Stock increase check: no increases since last run.")

    if new_listed_products:
        print(
            colorize(
                args,
                f"New listing check: {len(new_listed_products)} new product(s) detected.",
                ANSI_CYAN,
            )
        )
        for row in new_listed_products:
            print(
                colorize(
                    args,
                    "- {name} (PId {product_id}) stock={current_stock}".format(**row),
                    ANSI_CYAN,
                )
            )
            print(colorize(args, "  Detail: {detail_url}".format(**row), ANSI_CYAN))
            print(colorize(args, "  Search: {search_url}".format(**row), ANSI_CYAN))
    else:
        print("New listing check: no new products since last run.")

    if in_stock_count_increase:
        print(
            colorize(
                args,
                "In-stock count check: "
                "{previous_count} -> {current_count} (+{delta})".format(
                    **in_stock_count_increase
                ),
                ANSI_YELLOW,
            )
        )
        if in_stock_count_products:
            print("Products that moved into stock:")
            for row in in_stock_count_products:
                print(
                    "- {name} (PId {product_id}) {previous_stock} -> {current_stock} (+{delta})".format(
                        **row
                    )
                )
                print("  Detail: {detail_url}".format(**row))
                print("  Search: {search_url}".format(**row))
    else:
        print("In-stock count check: no increase since last run.")

    if args.watch_product_ids:
        if watched_in_stock_events:
            print(
                "Watched product transition check: "
                f"{len(watched_in_stock_events)} product(s) came in stock."
            )
            for row in watched_in_stock_events:
                print(
                    "- {name} (PId {product_id}) {previous_stock} -> {current_stock} (+{delta})".format(
                        **row
                    )
                )
        else:
            print("Watched product transition check: no new in-stock transitions.")


def main() -> None:
    args = parse_args()
    previous_stock_by_onsale: Dict[int, Dict[str, int]] = {
        onsale: {} for onsale in args.onsale_list
    }
    previous_in_stock_count_by_onsale: Dict[int, Optional[int]] = {
        onsale: None for onsale in args.onsale_list
    }
    previous_watch_stock_by_onsale: Dict[int, Dict[str, int]] = {
        onsale: {} for onsale in args.onsale_list
    }
    run_no = 0

    while args.max_runs == 0 or run_no < args.max_runs:
        run_no += 1
        print_run_header(run_no, args)
        for onsale_value in args.onsale_list:
            print(f"--- OnSale context: {onsale_value} ---")
            args.onsale = onsale_value
            previous_stock = previous_stock_by_onsale[onsale_value]
            previous_in_stock_count = previous_in_stock_count_by_onsale[onsale_value]
            previous_watch_stock = previous_watch_stock_by_onsale[onsale_value]

            try:
                products, total_count, pages_fetched = fetch_all_products(args)
                current_stock = build_stock_map(products)
                current_in_stock_count = count_in_stock_products(current_stock)
                in_stock_count_increase = detect_in_stock_count_increase(
                    previous_in_stock_count, current_in_stock_count
                )
                in_stock_count_products: List[Dict[str, object]] = []
                if in_stock_count_increase:
                    in_stock_count_products = detect_in_stock_count_products(
                        previous_stock, current_stock, products
                    )
                increases = detect_increases(previous_stock, current_stock, products)
                new_listed_products: List[Dict[str, object]] = []
                if previous_stock:
                    new_listed_products = detect_new_listed_products(
                        previous_stock,
                        current_stock,
                        products,
                        min_stock=args.new_product_min_stock,
                    )

                watched_rows: List[Dict[str, object]] = []
                watched_lookup_pages = 0
                watched_in_stock_events: List[Dict[str, object]] = []
                current_watch_stock: Dict[str, int] = {}

                if args.watch_product_ids:
                    watched_products, watched_lookup_pages = fetch_watched_products(
                        args, args.watch_product_ids
                    )
                    watched_rows, current_watch_stock = build_watched_rows(
                        args.watch_product_ids, watched_products
                    )
                    watched_in_stock_events = detect_watched_in_stock(
                        previous_watch_stock,
                        current_watch_stock,
                        watched_products,
                        args.watch_product_ids,
                    )

                print_run_summary(
                    args,
                    products,
                    total_count,
                    pages_fetched,
                    current_in_stock_count,
                    in_stock_count_increase,
                    in_stock_count_products,
                    increases,
                    new_listed_products,
                    watched_rows,
                    watched_lookup_pages,
                    watched_in_stock_events,
                )

                if (
                    increases
                    or new_listed_products
                    or watched_in_stock_events
                    or in_stock_count_increase
                ):
                    if can_send_email(args):
                        send_email_alert(
                            args=args,
                            increases=increases,
                            new_listed_products=new_listed_products,
                            watched_in_stock_events=watched_in_stock_events,
                            in_stock_count_increase=in_stock_count_increase,
                            in_stock_count_products=in_stock_count_products,
                            run_no=run_no,
                            total_products=len(products),
                            total_count=total_count,
                        )
                        print("Email alert: sent.")
                    else:
                        print(
                            "Email alert: skipped (set --smtp-host --email-from --email-to to enable)."
                        )

                previous_stock_by_onsale[onsale_value] = current_stock
                previous_in_stock_count_by_onsale[onsale_value] = current_in_stock_count
                if args.watch_product_ids:
                    previous_watch_stock_by_onsale[onsale_value] = current_watch_stock

            except Exception as exc:  # pylint: disable=broad-except
                print(f"Run error (OnSale={onsale_value}): {exc}")

        if args.max_runs and run_no >= args.max_runs:
            break

        sleep_seconds = random.randint(args.min_interval, args.max_interval)
        print(f"Sleeping {sleep_seconds} second(s)...")
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()
