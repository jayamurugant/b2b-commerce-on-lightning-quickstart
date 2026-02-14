#!/usr/bin/env python3
"""FirstCry stock monitor v2.

Adds on top of v1:
- Terminal alert sound (Windows-friendly) when an alert is triggered
- Twilio WhatsApp alert integration
- Optional notifier self-test mode

v1 behavior is preserved:
- Random polling interval
- Email alert support
- Overall in-stock count increase detection
- Watched product IDs support
"""

import argparse
import base64
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple

import firstcry_stock_monitor as v1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "FirstCry stock monitor v2 with email + Windows terminal sound + "
            "Twilio WhatsApp alerts."
        )
    )

    # Core listing params
    parser.add_argument("--brand-id", type=int, default=113, help="MasterBrand value")
    parser.add_argument("--onsale", type=int, default=0, help="OnSale value")
    parser.add_argument("--search-string", default="brand", help="SearchString value")
    parser.add_argument("--sort", default="Popularity", help="SortExpression")
    parser.add_argument("--page-size", type=int, default=20, help="PageSize")
    parser.add_argument("--pincode", default="0", help="pcode value")
    parser.add_argument(
        "--product-ids",
        default="",
        help="Comma-separated Product IDs to explicitly track",
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

    # Email config (same as v1)
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

    # Sound config
    parser.add_argument(
        "--disable-sound",
        action="store_true",
        help="Disable terminal prompt sound on alert",
    )
    parser.add_argument(
        "--sound-repeat",
        type=int,
        default=2,
        help="How many chimes to play on alert",
    )

    # Twilio WhatsApp config
    parser.add_argument("--twilio-account-sid", default="", help="Twilio Account SID")
    parser.add_argument("--twilio-auth-token", default="", help="Twilio Auth Token")
    parser.add_argument(
        "--twilio-from-whatsapp",
        default="",
        help='Twilio WhatsApp sender, e.g. "whatsapp:+14155238886"',
    )
    parser.add_argument(
        "--twilio-to-whatsapp",
        default="",
        help='Comma-separated recipients, e.g. "whatsapp:+9198...,whatsapp:+1..."',
    )
    parser.add_argument(
        "--twilio-timeout",
        type=int,
        default=30,
        help="HTTP timeout seconds for Twilio API call",
    )

    # Utility
    parser.add_argument(
        "--test-notifiers",
        action="store_true",
        help="Send a one-time test alert to configured notifiers and exit",
    )

    args = parser.parse_args()
    if args.min_interval > args.max_interval:
        parser.error("--min-interval cannot be greater than --max-interval")
    if args.sound_repeat < 1:
        parser.error("--sound-repeat must be >= 1")

    args.watch_product_ids = v1.parse_product_ids(args.product_ids)
    args.twilio_recipients = parse_whatsapp_recipients(args.twilio_to_whatsapp)
    args.twilio_from_whatsapp = normalize_whatsapp(args.twilio_from_whatsapp)
    try:
        v1.attach_proxy_rotator(args)
    except Exception as exc:
        parser.error(str(exc))
    return args


def normalize_whatsapp(value: str) -> str:
    item = value.strip()
    if not item:
        return ""
    if item.lower().startswith("whatsapp:"):
        return item
    return f"whatsapp:{item}"


def parse_whatsapp_recipients(value: str) -> List[str]:
    recipients = v1.parse_recipients(value)
    return [normalize_whatsapp(item) for item in recipients]


def can_send_whatsapp(args: argparse.Namespace) -> bool:
    return bool(
        args.twilio_account_sid
        and args.twilio_auth_token
        and args.twilio_from_whatsapp
        and args.twilio_recipients
    )


def play_alert_sound(args: argparse.Namespace) -> None:
    if args.disable_sound:
        return

    # Windows first: use winsound for a reliable laptop prompt sound.
    try:
        import winsound  # type: ignore

        for _ in range(args.sound_repeat):
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            time.sleep(0.15)
        print("Alert sound: played (winsound).")
        return
    except Exception:
        pass

    # Fallback: terminal bell.
    try:
        print("\a", end="", flush=True)
        print("Alert sound: terminal bell sent.")
    except Exception as exc:
        print(f"Alert sound error: {exc}")


def build_compact_alert_text(
    args: argparse.Namespace,
    run_no: int,
    total_products: int,
    total_count: Optional[int],
    increases: List[Dict[str, object]],
    watched_in_stock_events: List[Dict[str, object]],
    in_stock_count_increase: Optional[Dict[str, int]],
) -> str:
    lines: List[str] = [
        f"{args.email_subject_prefix} v2",
        f"Run #{run_no}",
        (
            f"Brand={args.brand_id} OnSale={args.onsale} Pincode={args.pincode} "
            f"Fetched={total_products} Catalog={total_count if total_count is not None else 'NA'}"
        ),
    ]

    if in_stock_count_increase:
        lines.append(
            "In-stock count: {previous_count}->{current_count} (+{delta})".format(
                **in_stock_count_increase
            )
        )

    if increases:
        lines.append(f"Stock increases: {len(increases)}")
        for row in increases[:8]:
            lines.append(
                "- PId {product_id}: {previous_stock}->{current_stock} (+{delta})".format(
                    **row
                )
            )
        if len(increases) > 8:
            lines.append(f"... and {len(increases) - 8} more")

    if watched_in_stock_events:
        lines.append(f"Watched now in stock: {len(watched_in_stock_events)}")
        for row in watched_in_stock_events[:8]:
            lines.append(
                "- PId {product_id}: {previous_stock}->{current_stock} (+{delta})".format(
                    **row
                )
            )
        if len(watched_in_stock_events) > 8:
            lines.append(f"... and {len(watched_in_stock_events) - 8} more")

    return "\n".join(lines)


def send_twilio_whatsapp(
    args: argparse.Namespace, message_body: str
) -> Tuple[List[str], Dict[str, str]]:
    url = (
        "https://api.twilio.com/2010-04-01/Accounts/"
        f"{args.twilio_account_sid}/Messages.json"
    )
    auth_raw = f"{args.twilio_account_sid}:{args.twilio_auth_token}".encode("utf-8")
    auth_header = f"Basic {base64.b64encode(auth_raw).decode('utf-8')}"

    successful: List[str] = []
    failed: Dict[str, str] = {}

    for recipient in args.twilio_recipients:
        form_data = urllib.parse.urlencode(
            {
                "From": args.twilio_from_whatsapp,
                "To": recipient,
                "Body": message_body,
            }
        ).encode("utf-8")

        request = urllib.request.Request(
            url=url,
            data=form_data,
            method="POST",
            headers={
                "Authorization": auth_header,
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "firstcry-stock-monitor-v2",
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=args.twilio_timeout) as response:
                _ = response.read().decode("utf-8", errors="replace")
                successful.append(recipient)
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            failed[recipient] = f"HTTP {exc.code}: {details[:250]}"
        except Exception as exc:  # pylint: disable=broad-except
            failed[recipient] = str(exc)

    return successful, failed


def send_test_notifiers(args: argparse.Namespace) -> None:
    print("Running notifier self-test...")
    fake_increase = [
        {
            "product_id": "12345678",
            "name": "Test Product",
            "previous_stock": 0,
            "current_stock": 3,
            "delta": 3,
        }
    ]
    fake_count = {"previous_count": 10, "current_count": 12, "delta": 2}

    if v1.can_send_email(args):
        v1.send_email_alert(
            args=args,
            increases=fake_increase,
            watched_in_stock_events=[],
            in_stock_count_increase=fake_count,
            run_no=0,
            total_products=20,
            total_count=100,
        )
        print("Test email: sent.")
    else:
        print("Test email: skipped (email config missing).")

    play_alert_sound(args)

    if can_send_whatsapp(args):
        text = build_compact_alert_text(
            args=args,
            run_no=0,
            total_products=20,
            total_count=100,
            increases=fake_increase,
            watched_in_stock_events=[],
            in_stock_count_increase=fake_count,
        )
        success, failed = send_twilio_whatsapp(args, text)
        print(f"Test WhatsApp: sent={len(success)} failed={len(failed)}")
        for to in success:
            print(f"- success: {to}")
        for to, err in failed.items():
            print(f"- failed: {to} | {err}")
    else:
        print("Test WhatsApp: skipped (Twilio config missing).")


def main() -> None:
    args = parse_args()

    if args.test_notifiers:
        send_test_notifiers(args)
        return

    previous_stock: Dict[str, int] = {}
    previous_in_stock_count: Optional[int] = None
    previous_watch_stock: Dict[str, int] = {}
    run_no = 0

    while args.max_runs == 0 or run_no < args.max_runs:
        run_no += 1
        v1.print_run_header(run_no, args)

        try:
            products, total_count, pages_fetched = v1.fetch_all_products(args)
            current_stock = v1.build_stock_map(products)
            current_in_stock_count = v1.count_in_stock_products(current_stock)
            in_stock_count_increase = v1.detect_in_stock_count_increase(
                previous_in_stock_count, current_in_stock_count
            )
            increases = v1.detect_increases(previous_stock, current_stock, products)

            watched_rows: List[Dict[str, object]] = []
            watched_lookup_pages = 0
            watched_in_stock_events: List[Dict[str, object]] = []
            current_watch_stock: Dict[str, int] = {}

            if args.watch_product_ids:
                watched_products, watched_lookup_pages = v1.fetch_watched_products(
                    args, args.watch_product_ids
                )
                watched_rows, current_watch_stock = v1.build_watched_rows(
                    args.watch_product_ids, watched_products
                )
                watched_in_stock_events = v1.detect_watched_in_stock(
                    previous_watch_stock,
                    current_watch_stock,
                    watched_products,
                    args.watch_product_ids,
                )

            v1.print_run_summary(
                args=args,
                products=products,
                total_count=total_count,
                pages_fetched=pages_fetched,
                current_in_stock_count=current_in_stock_count,
                in_stock_count_increase=in_stock_count_increase,
                increases=increases,
                watched_rows=watched_rows,
                watched_lookup_pages=watched_lookup_pages,
                watched_in_stock_events=watched_in_stock_events,
            )

            has_alert = bool(
                increases or watched_in_stock_events or in_stock_count_increase
            )
            if has_alert:
                if v1.can_send_email(args):
                    v1.send_email_alert(
                        args=args,
                        increases=increases,
                        watched_in_stock_events=watched_in_stock_events,
                        in_stock_count_increase=in_stock_count_increase,
                        run_no=run_no,
                        total_products=len(products),
                        total_count=total_count,
                    )
                    print("Email alert: sent.")
                else:
                    print(
                        "Email alert: skipped (set --smtp-host --email-from --email-to)."
                    )

                play_alert_sound(args)

                if can_send_whatsapp(args):
                    body = build_compact_alert_text(
                        args=args,
                        run_no=run_no,
                        total_products=len(products),
                        total_count=total_count,
                        increases=increases,
                        watched_in_stock_events=watched_in_stock_events,
                        in_stock_count_increase=in_stock_count_increase,
                    )
                    sent, failed = send_twilio_whatsapp(args, body)
                    print(
                        f"WhatsApp alert: sent={len(sent)} "
                        f"failed={len(failed)}"
                    )
                    for to, err in failed.items():
                        print(f"- WhatsApp failed: {to} | {err}")
                else:
                    print(
                        "WhatsApp alert: skipped "
                        "(set Twilio SID/token/from/to params)."
                    )

            previous_stock = current_stock
            previous_in_stock_count = current_in_stock_count
            if args.watch_product_ids:
                previous_watch_stock = current_watch_stock

        except Exception as exc:  # pylint: disable=broad-except
            print(f"Run error: {exc}")

        if args.max_runs and run_no >= args.max_runs:
            break

        sleep_seconds = random.randint(args.min_interval, args.max_interval)
        print(f"Sleeping {sleep_seconds} second(s)...")
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()
