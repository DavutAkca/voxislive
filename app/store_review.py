"""Ask a Store customer to rate Voxis, at a moment the app has just earned it.

Why this exists: the Store surfaces a star rating **per market**, and only once a
market has collected a handful of them — an app with two ratings spread across
two countries shows stars in neither. Voxis ships with none, so the first few
ratings in a single market are worth more than the hundredth anywhere.

Why a deep link rather than the in-app dialog: `StoreContext.RequestRateAndReview
AppAsync` needs package identity *and* an owner window (IInitializeWithWindow) in
a Win32/Desktop-Bridge process. Wiring that from Python is fragile, and the
failure mode is a modal dialog stranded behind the app — worse than one extra
click. The `ms-windows-store://review` protocol lands on the same rating sheet,
needs no dependency, and cannot hang the UI. The browser extension already asks
this way.

Never incentivised. Trading anything for a rating breaches Store policy, and a
bought rating is worth less than no rating.
"""
import logging
import os

from . import paths

PRODUCT_ID = "9P5Z0KVS58RS"
REVIEW_URI = f"ms-windows-store://review/?ProductId={PRODUCT_ID}"

log = logging.getLogger("voxis")


def available() -> bool:
    """Only the Store build can be rated in the Store. The sideloaded .exe and a
    source run have no listing behind them, and prompting there would send the
    user to a page that cannot accept their rating."""
    return paths.is_store_build()


def open_review_page() -> bool:
    """Open the Store on Voxis's rating sheet. Best-effort: a missing Store app
    or a blocked protocol handler must not surface as an error to someone who was
    only being asked for a favour."""
    if not available():
        return False
    try:
        os.startfile(REVIEW_URI)  # noqa: S606 - fixed ms-windows-store: URI
        return True
    except OSError as exc:
        log.info("store review: could not open the Store (%s)", exc)
        return False
