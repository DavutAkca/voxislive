"""Check remaining Alibaba Cloud resource-package quota (e.g. DashScope free tier).

Standalone ops utility, unrelated to the app runtime — queries the BSS
(Billing) OpenAPI's QueryResourcePackageInstances for a product code (default
"dashscope") and prints TotalAmount / RemainingAmount / consumed per package.

Requires an Alibaba Cloud AccessKey pair with the `bss:DescribeInstances`
permission (the AliyunBSSReadOnlyAccess system policy covers it) — this is a
SEPARATE credential from the DashScope API key used by the app; it must never
be the app's `qwen_api_key`, and must never be hardcoded or committed. Read
only from the environment:

    ALIBABA_CLOUD_ACCESS_KEY_ID
    ALIBABA_CLOUD_ACCESS_KEY_SECRET

Usage:
    python scripts/check_dashscope_quota.py [product_code]

Install:
    pip install alibabacloud_bssopenapi20171214 alibabacloud_tea_openapi alibabacloud_tea_util
"""
import os
import sys

from Tea.exceptions import TeaException, UnretryableException
from alibabacloud_bssopenapi20171214 import models as bss_models
from alibabacloud_bssopenapi20171214.client import Client as BssOpenApiClient
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_util import models as util_models

ENDPOINT = "business.aliyuncs.com"
PAGE_SIZE = 100


class AliyunBillingChecker:
    def __init__(self):
        access_key_id = os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_ID")
        access_key_secret = os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_SECRET")
        if not access_key_id or not access_key_secret:
            raise ValueError(
                "Set ALIBABA_CLOUD_ACCESS_KEY_ID and ALIBABA_CLOUD_ACCESS_KEY_SECRET "
                "in the environment (never hardcode credentials)."
            )
        config = open_api_models.Config(
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            endpoint=ENDPOINT,
        )
        self.client = BssOpenApiClient(config)

    def _fetch_instances(self, product_code):
        """Page through QueryResourcePackageInstances; returns a flat instance list."""
        instances = []
        page_num = 1
        runtime = util_models.RuntimeOptions()
        while True:
            request = bss_models.QueryResourcePackageInstancesRequest(
                product_code=product_code or None,
                page_num=page_num,
                page_size=PAGE_SIZE,
            )
            response = self.client.query_resource_package_instances_with_options(request, runtime)
            data = response.body.data
            batch = (data.instances.instance if data and data.instances else None) or []
            instances.extend(batch)
            total = getattr(data, "total_count", len(instances)) or 0
            if len(instances) >= total or not batch:
                break
            page_num += 1
        return instances

    def get_remaining_quota(self, product_code="dashscope"):
        try:
            instances = self._fetch_instances(product_code)
            if not instances and product_code:
                print(f"No packages found for ProductCode='{product_code}'. "
                      f"Retrying without a product filter and matching client-side...\n")
                instances = [
                    inst for inst in self._fetch_instances(None)
                    if _matches_product(inst, product_code)
                ]
            self._print_table(instances)
        except (TeaException, UnretryableException) as error:
            code = getattr(error, "code", "Unknown")
            message = getattr(error, "message", str(error))
            print(f"Alibaba Cloud API error [{code}]: {message}", file=sys.stderr)
            if code in ("Forbidden.RAM", "NoPermission"):
                print("Hint: the AccessKey needs the 'bss:DescribeInstances' permission "
                      "(AliyunBSSReadOnlyAccess system policy).", file=sys.stderr)
            sys.exit(1)
        except Exception as error:  # network errors, malformed config, etc.
            print(f"Unexpected error: {error}", file=sys.stderr)
            sys.exit(1)

    @staticmethod
    def _print_table(instances):
        if not instances:
            print("No resource package instances found on this account.")
            return

        headers = ["PackageType", "Status", "Total", "Remaining", "Used", "Valid", "Expires"]
        rows = []
        for inst in instances:
            total = _to_float(inst.total_amount)
            remaining = _to_float(inst.remaining_amount)
            used = None if total is None or remaining is None else total - remaining
            unit = inst.remaining_amount_unit or inst.total_amount_unit or ""
            rows.append([
                inst.package_type or "-",
                inst.status or "-",
                _fmt(total, unit),
                _fmt(remaining, unit),
                _fmt(used, unit),
                inst.effective_time or "-",
                inst.expiry_time or "-",
            ])

        widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
        sep = "-+-".join("-" * w for w in widths)

        def fmt_row(cells):
            return " | ".join(c.ljust(w) for c, w in zip(cells, widths))

        print(fmt_row(headers))
        print(sep)
        for inst, row in zip(instances, rows):
            print(fmt_row(row))
            remaining = _to_float(inst.remaining_amount)
            total = _to_float(inst.total_amount)
            if remaining is not None and total and total > 0 and remaining / total <= 0.2:
                print(f"  ^ LOW QUOTA: only {remaining / total:.0%} of '{inst.package_type}' remains")


def _matches_product(instance, product_code):
    products = getattr(instance, "applicable_products", None) or []
    return any(product_code.lower() in str(p).lower() for p in products)


def _to_float(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _fmt(value, unit):
    if value is None:
        return "-"
    return f"{value:g} {unit}".strip()


if __name__ == "__main__":
    product_arg = sys.argv[1] if len(sys.argv) > 1 else "dashscope"
    checker = AliyunBillingChecker()
    checker.get_remaining_quota(product_arg)
