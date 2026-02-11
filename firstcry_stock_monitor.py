#!/usr/bin/env python3
"""Console stock monitor for FirstCry brand listing APIs.

Features:
- Polls listing API every random interval (default 30-45 seconds)
- Prints summary to console only (no file writes)
- Tracks previous stock in memory
- Sends email alert when any product's stock increases versus prior poll
"""

import argparse
import datetime as dt
import json
import random
import smtplib
import time
import urllib.parse
import urllib.request
from email.message import EmailMessage
from typing import Dict, List, Optional, Tuple


BASE_API = "https://www.firstcry.com/svcs/SearchResult.svc"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor FirstCry stock changes and email on stock increases."
    )

    # Listing params for URL pattern like /hotwheels/0/0/113
    parser.add_argument("--brand-id", type=int, default=113, help="MasterBrand value")
    parser.add_argument("--onsale", type=int, default=0, help="OnSale value")
    parser.add_argument(
        "--search-string", default="brand", help="SearchString API value"
    )
    parser.add_argument("--sort", default="Popularity", help="SortExpression")
    parser.add_argument("--page-size", type=int, default=20, help="PageSize")
    parser.add_argument("--pincode", default="0", help="pcode value")
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


def build_params(args: argparse.Namespace, page_no: int) -> Dict[str, str]:
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
    if args.exclude_out_of_stock:
        params["OutOfStock"] = "0"
    return params


def endpoint_for_page(page_no: int) -> str:
    if page_no == 1:
        return "GetSearchResultProductsFilters"
    return "GetSearchResultProductsPaging"


def request_listing(args: argparse.Namespace, page_no: int) -> Dict[str, object]:
    endpoint = endpoint_for_page(page_no)
    params = build_params(args, page_no)
    url = f"{BASE_API}/{endpoint}?{urllib.parse.urlencode(params)}"

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": args.referer,
    }

    req = urllib.request.Request(url, headers=headers)
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
) -> Tuple[List[Dict[str, object]], Optional[int], int]:
    all_products: List[Dict[str, object]] = []
    total_count: Optional[int] = None
    pages_fetched = 0

    for page_no in range(1, args.max_pages + 1):
        inner = request_listing(args, page_no)
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
            increases.append(
                {
                    "product_id": pid,
                    "name": str(item.get("PNm", "")),
                    "previous_stock": prev_stock,
                    "current_stock": current_stock,
                    "delta": current_stock - prev_stock,
                    "url": item.get("Purl", ""),
                }
            )
    return increases


def parse_recipients(value: str) -> List[str]:
    return [addr.strip() for addr in value.split(",") if addr.strip()]


def can_send_email(args: argparse.Namespace) -> bool:
    recipients = parse_recipients(args.email_to)
    return bool(args.smtp_host and args.email_from and recipients)


def send_email_alert(
    args: argparse.Namespace,
    increases: List[Dict[str, object]],
    run_no: int,
    total_products: int,
    total_count: Optional[int],
) -> None:
    recipients = parse_recipients(args.email_to)
    subject = (
        f"{args.email_subject_prefix} "
        f"{len(increases)} stock increase(s) detected"
    )

    lines = [
        "Stock increase detected in FirstCry monitor.",
        f"Run: {run_no}",
        f"Products fetched: {total_products}",
        f"Catalog count: {total_count if total_count is not None else 'NA'}",
        "",
        "Changed products:",
    ]
    for row in increases:
        lines.append(
            "- {name} (PId {product_id}): {previous_stock} -> {current_stock} (+{delta})".format(
                **row
            )
        )

    message = EmailMessage()
    message["From"] = args.email_from
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message.set_content("\n".join(lines))

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


def print_run_header(run_no: int) -> None:
    ts = dt.datetime.now().isoformat(timespec="seconds")
    print("=" * 80)
    print(f"[{ts}] Poll run #{run_no}")


def print_run_summary(
    products: List[Dict[str, object]],
    total_count: Optional[int],
    pages_fetched: int,
    increases: List[Dict[str, object]],
) -> None:
    print(f"Pages fetched: {pages_fetched}")
    print(
        f"Products fetched: {len(products)} | Reported total count: "
        f"{total_count if total_count is not None else 'NA'}"
    )

    preview = products[:5]
    if preview:
        print("Top products:")
        for item in preview:
            pid = item.get("PId")
            name = item.get("PNm")
            stock = to_int(item.get("CrntStock"), default=0)
            print(f"- {name} (PId {pid}) stock={stock}")

    if not increases:
        print("Stock increase check: no increases since last run.")
        return

    print(f"Stock increase check: {len(increases)} product(s) increased.")
    for row in increases:
        print(
            "- {name} (PId {product_id}) {previous_stock} -> {current_stock} (+{delta})".format(
                **row
            )
        )


def main() -> None:
    args = parse_args()
    previous_stock: Dict[str, int] = {}
    run_no = 0

    while args.max_runs == 0 or run_no < args.max_runs:
        run_no += 1
        print_run_header(run_no)

        try:
            products, total_count, pages_fetched = fetch_all_products(args)
            current_stock = build_stock_map(products)
            increases = detect_increases(previous_stock, current_stock, products)
            print_run_summary(products, total_count, pages_fetched, increases)

            if increases:
                if can_send_email(args):
                    send_email_alert(
                        args=args,
                        increases=increases,
                        run_no=run_no,
                        total_products=len(products),
                        total_count=total_count,
                    )
                    print("Email alert: sent.")
                else:
                    print(
                        "Email alert: skipped (set --smtp-host --email-from --email-to to enable)."
                    )

            previous_stock = current_stock

        except Exception as exc:  # pylint: disable=broad-except
            print(f"Run error: {exc}")

        if args.max_runs and run_no >= args.max_runs:
            break

        sleep_seconds = random.randint(args.min_interval, args.max_interval)
        print(f"Sleeping {sleep_seconds} second(s)...")
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()
