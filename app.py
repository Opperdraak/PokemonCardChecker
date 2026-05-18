from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "history.db"
EBAY_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_BROWSE_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
POKEMON_TCG_CARDS_URL = "https://api.pokemontcg.io/v2/cards"
EBAY_SCOPE = "https://api.ebay.com/oauth/api_scope"


class AppError(Exception):
    """Base exception for user-facing application errors."""


class ExternalAPIError(AppError):
    """Raised when an upstream API call fails."""


def load_env_file() -> None:
    """Load credentials from the local .env file."""
    load_dotenv(dotenv_path=BASE_DIR / ".env", override=True)


def get_optional_env(name: str) -> str | None:
    value = os.getenv(name, "").strip()

    if not value or value.upper() in {"PENDING", "(PENDING)"}:
        return None
    return value


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS search_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                searched_at TEXT NOT NULL,
                card_name TEXT NOT NULL,
                card_number TEXT NOT NULL,
                ebay_avg_price REAL,
                cardmarket_trend_price REAL,
                cardmarket_average_sell_price REAL,
                cardmarket_low_price REAL
            )
            """
        )
        connection.commit()


def log_search(
    *,
    card_name: str,
    card_number: str,
    ebay_avg_price: float | None,
    cardmarket_trend_price: float | None,
    cardmarket_average_sell_price: float | None,
    cardmarket_low_price: float | None,
) -> None:
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute(
            """
            INSERT INTO search_history (
                searched_at,
                card_name,
                card_number,
                ebay_avg_price,
                cardmarket_trend_price,
                cardmarket_average_sell_price,
                cardmarket_low_price
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now().astimezone().isoformat(timespec="seconds"),
                card_name,
                card_number,
                ebay_avg_price,
                cardmarket_trend_price,
                cardmarket_average_sell_price,
                cardmarket_low_price,
            ),
        )
        connection.commit()


def fetch_history(limit: int = 15) -> list[dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT
                id,
                searched_at,
                card_name,
                card_number,
                ebay_avg_price,
                cardmarket_trend_price,
                cardmarket_average_sell_price,
                cardmarket_low_price
            FROM search_history
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


@st.cache_data(ttl=6900, show_spinner=False)
def get_ebay_access_token(client_id: str, client_secret: str) -> str:
    response = requests.post(
        EBAY_TOKEN_URL,
        auth=HTTPBasicAuth(client_id, client_secret),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials", "scope": EBAY_SCOPE},
        timeout=30,
    )
    if not response.ok:
        raise ExternalAPIError(
            f"eBay OAuth request failed with status {response.status_code}: {response.text}"
        )

    payload = response.json()
    access_token = payload.get("access_token")
    if not access_token:
        raise ExternalAPIError("eBay OAuth response did not include an access token.")
    return access_token


def money_value_to_float(money: dict[str, Any] | None) -> float | None:
    if not money:
        return None

    raw_value = money.get("value")
    if raw_value in (None, ""):
        return None

    try:
        return float(raw_value)
    except (TypeError, ValueError):
        return None


def extract_shipping_cost(item: dict[str, Any]) -> tuple[float | None, str | None]:
    shipping_options = item.get("shippingOptions") or []
    if not shipping_options:
        return None, None

    shipping_cost = money_value_to_float(shipping_options[0].get("shippingCost"))
    shipping_currency = (shipping_options[0].get("shippingCost") or {}).get("currency")
    return shipping_cost, shipping_currency


def search_ebay_listings(query: str, access_token: str, limit: int = 5) -> list[dict[str, Any]]:
    response = requests.get(
        EBAY_BROWSE_SEARCH_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB",
        },
        params={
            "q": query,
            "limit": limit,
            "filter": "itemLocationCountry:GB",
        },
        timeout=30,
    )
    if not response.ok:
        raise ExternalAPIError(
            f"eBay Browse search failed with status {response.status_code}: {response.text}"
        )

    item_summaries = response.json().get("itemSummaries", [])
    listings: list[dict[str, Any]] = []
    for item in item_summaries[:limit]:
        price_info = item.get("price") or {}
        price_value = money_value_to_float(price_info)
        price_currency = price_info.get("currency")
        shipping_value, shipping_currency = extract_shipping_cost(item)
        total_value = (
            price_value + shipping_value
            if price_value is not None and shipping_value is not None
            else price_value
        )

        listings.append(
            {
                "Title": item.get("title", "Untitled listing"),
                "Price": price_value,
                "Price Currency": price_currency,
                "Shipping": shipping_value,
                "Shipping Currency": shipping_currency or price_currency,
                "Total": total_value,
                "Total Currency": price_currency,
                "Listing URL": item.get("itemWebUrl"),
            }
        )

    return listings


def escape_query_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def normalize(value: str) -> str:
    return " ".join(value.strip().lower().split())


def parse_card_number(value: str) -> dict[str, Any]:
    cleaned = value.strip().upper().replace(" ", "")
    base = cleaned.split("/", 1)[0]
    match = re.fullmatch(r"([A-Z]*)(\d+)", base)

    if not match:
        return {
            "raw": cleaned,
            "base": base,
            "prefix": "",
            "digits": None,
        }

    prefix, digits = match.groups()
    return {
        "raw": cleaned,
        "base": base,
        "prefix": prefix,
        "digits": int(digits),
    }


def normalized_card_number_candidates(value: str) -> set[str]:
    parts = parse_card_number(value)
    cleaned = parts["raw"]
    if not cleaned:
        return set()

    candidates = {cleaned, parts["base"]}

    if "/" in cleaned:
        candidates.add(cleaned.split("/", 1)[0])

    expanded = set(candidates)
    for candidate in list(candidates):
        if candidate.isdigit():
            expanded.add(str(int(candidate)))
            continue

        match = re.fullmatch(r"([A-Z]+)0*(\d+)", candidate)
        if match:
            prefix, digits = match.groups()
            expanded.add(f"{prefix}{int(digits)}")
            expanded.add(str(int(digits)))

    return {candidate for candidate in expanded if candidate}


def card_number_matches(user_number: str, api_number: str) -> bool:
    user_candidates = normalized_card_number_candidates(user_number)
    api_candidates = normalized_card_number_candidates(api_number)
    return bool(user_candidates and api_candidates and user_candidates & api_candidates)


def score_card_number_similarity(user_number: str, api_number: str) -> int:
    if card_number_matches(user_number, api_number):
        return 1_000

    user_parts = parse_card_number(user_number)
    api_parts = parse_card_number(api_number)
    score = int(
        SequenceMatcher(None, user_parts["raw"], api_parts["raw"]).ratio() * 300
    )

    user_digits = user_parts["digits"]
    api_digits = api_parts["digits"]
    if user_digits is not None and api_digits is not None:
        difference = abs(user_digits - api_digits)
        score += max(0, 240 - min(difference, 24) * 10)

    user_prefix = user_parts["prefix"]
    api_prefix = api_parts["prefix"]
    if user_prefix and api_prefix and user_prefix == api_prefix:
        score += 60
    elif not user_prefix or not api_prefix:
        score += 30

    return score


def score_card_name_similarity(user_name: str, api_name: str) -> int:
    normalized_user_name = normalize(user_name)
    normalized_api_name = normalize(api_name)

    if normalized_user_name == normalized_api_name:
        return 200
    if normalized_user_name in normalized_api_name or normalized_api_name in normalized_user_name:
        return 150
    return int(SequenceMatcher(None, normalized_user_name, normalized_api_name).ratio() * 100)


def build_candidate_label(candidate: dict[str, Any]) -> str:
    parts = [f'{candidate["name"]} #{candidate["number"]}']
    if candidate.get("set_name"):
        parts.append(candidate["set_name"])
    if candidate.get("release_date"):
        parts.append(candidate["release_date"])
    return " | ".join(parts)


def build_card_candidate(
    card: dict[str, Any],
    requested_name: str,
    requested_number: str,
) -> dict[str, Any]:
    cardmarket = card.get("cardmarket") or {}
    prices = cardmarket.get("prices") or {}
    card_number = card.get("number", "")
    candidate = {
        "id": card.get("id"),
        "name": card.get("name"),
        "number": card_number,
        "set_name": (card.get("set") or {}).get("name"),
        "release_date": (card.get("set") or {}).get("releaseDate"),
        "cardmarket_url": cardmarket.get("url"),
        "cardmarket_updated_at": cardmarket.get("updatedAt"),
        "trend_price": prices.get("trendPrice"),
        "average_sell_price": prices.get("averageSellPrice"),
        "low_price": prices.get("lowPrice"),
        "is_exact_name_match": normalize(card.get("name", "")) == normalize(requested_name),
        "is_exact_number_match": card_number_matches(requested_number, card_number),
    }
    candidate["match_score"] = (
        score_card_number_similarity(requested_number, card_number) * 10
        + score_card_name_similarity(requested_name, card.get("name", ""))
    )
    candidate["label"] = build_candidate_label(candidate)
    return candidate


def choose_best_candidate(
    requested_name: str,
    requested_number: str,
    cards: list[dict[str, Any]],
    preferred_card_id: str | None = None,
) -> dict[str, Any]:
    candidates = [
        build_card_candidate(card, requested_name, requested_number)
        for card in cards
    ]
    candidates.sort(
        key=lambda candidate: (
            candidate["match_score"],
            candidate.get("release_date") or "",
            candidate["label"],
        ),
        reverse=True,
    )

    visible_candidates = candidates[:5]
    selected_candidate = visible_candidates[0]
    exact_candidate_count = sum(
        1
        for candidate in visible_candidates
        if candidate["is_exact_name_match"] and candidate["is_exact_number_match"]
    )
    selection_mode = (
        "exact"
        if selected_candidate["is_exact_name_match"]
        and selected_candidate["is_exact_number_match"]
        else "closest"
    )

    if preferred_card_id:
        preferred_candidate = next(
            (candidate for candidate in visible_candidates if candidate["id"] == preferred_card_id),
            None,
        )
        if preferred_candidate:
            selected_candidate = preferred_candidate
            selection_mode = "manual"

    selection_note = None
    show_candidate_options = selection_mode != "exact" or exact_candidate_count > 1

    if exact_candidate_count > 1:
        selection_note = (
            "Multiple close Cardmarket matches were found. "
            "Choose the exact card below if needed."
        )
    elif selection_mode == "closest":
        selection_note = (
            "Closest Cardmarket match selected automatically. "
            "Choose a different option below if needed."
        )
    elif selection_mode == "manual":
        selection_note = "Using your selected Cardmarket candidate."

    return {
        "selected_card": selected_candidate,
        "candidate_cards": visible_candidates,
        "selection_mode": selection_mode,
        "selection_note": selection_note,
        "show_candidate_options": show_candidate_options,
    }


def lookup_pokemon_card(
    card_name: str,
    card_number: str,
    api_key: str,
    preferred_card_id: str | None = None,
) -> dict[str, Any]:
    query = f'name:"{escape_query_value(card_name)}"'
    response = requests.get(
        POKEMON_TCG_CARDS_URL,
        headers={
            "X-Api-Key": api_key,
            "Accept": "application/json",
        },
        params={
            "q": query,
            "pageSize": 100,
            "orderBy": "-set.releaseDate",
        },
        timeout=30,
    )
    if not response.ok:
        raise ExternalAPIError(
            "Pokemon TCG card lookup failed with "
            f"status {response.status_code}: {response.text}"
        )

    cards = response.json().get("data", [])
    if not cards:
        raise ExternalAPIError(
            f'No Pokemon TCG card was found for "{card_name}" with number "{card_number}".'
        )

    return choose_best_candidate(
        card_name,
        card_number,
        cards,
        preferred_card_id=preferred_card_id,
    )


def compute_average_total_price(listings: list[dict[str, Any]]) -> float | None:
    totals = [item["Total"] for item in listings if item.get("Total") is not None]
    if not totals:
        return None
    return round(sum(totals) / len(totals), 2)


def format_currency(value: float | None, currency: str) -> str:
    if value is None:
        return "N/A"

    symbols = {"GBP": "GBP", "EUR": "EUR"}
    prefix = symbols.get(currency, currency)
    return f"{prefix} {value:,.2f}"


def format_history_label(entry: dict[str, Any]) -> str:
    timestamp = entry["searched_at"]
    try:
        dt = datetime.fromisoformat(timestamp)
        timestamp = dt.strftime("%d %b %H:%M")
    except ValueError:
        pass
    return f'{entry["card_name"]} #{entry["card_number"]} ({timestamp})'


def render_sidebar(history: list[dict[str, Any]]) -> None:
    st.sidebar.header("Past Searches")
    st.sidebar.caption("Click any search to repopulate the form and run it again.")

    if not history:
        st.sidebar.info("No searches logged yet.")
        return

    for entry in history:
        if st.sidebar.button(
            format_history_label(entry),
            key=f'history-search-{entry["id"]}',
            use_container_width=True,
        ):
            st.session_state["pending_history_search"] = {
                "card_name": entry["card_name"],
                "card_number": entry["card_number"],
            }
            st.rerun()


def render_metrics(card_data: dict[str, Any], ebay_average: float | None) -> None:
    trend_col, average_col, low_col, ebay_col = st.columns(4)
    trend_col.metric("Trend Price", format_currency(card_data["trend_price"], "EUR"))
    average_col.metric(
        "Average Sell",
        format_currency(card_data["average_sell_price"], "EUR"),
    )
    low_col.metric("Low Price", format_currency(card_data["low_price"], "EUR"))
    ebay_col.metric("eBay Avg Total", format_currency(ebay_average, "GBP"))

    details = []
    if card_data.get("set_name"):
        details.append(f'Set: {card_data["set_name"]}')
    if card_data.get("release_date"):
        details.append(f'Release Date: {card_data["release_date"]}')
    if card_data.get("cardmarket_updated_at"):
        details.append(f'Cardmarket Updated: {card_data["cardmarket_updated_at"]}')
    if details:
        st.caption(" | ".join(details))
    if card_data.get("cardmarket_url"):
        st.markdown(f'[Open Cardmarket record]({card_data["cardmarket_url"]})')


def queue_candidate_selection() -> None:
    st.session_state["preferred_card_id"] = st.session_state["candidate_choice"]
    st.session_state["run_search"] = True


def render_card_candidates(result: dict[str, Any]) -> None:
    candidate_cards = result.get("card_candidates", [])
    selected_card_id = result.get("selected_card_id")
    selection_mode = result.get("card_selection_mode")
    selection_note = result.get("card_selection_note")
    show_candidate_options = result.get("show_candidate_options", False)

    if selection_note:
        st.info(selection_note)

    if len(candidate_cards) < 2 or not show_candidate_options:
        return

    st.subheader("Possible Cardmarket Matches")

    candidate_map = {candidate["id"]: candidate for candidate in candidate_cards}
    candidate_ids = list(candidate_map)

    if st.session_state.get("candidate_choice") not in candidate_ids:
        st.session_state["candidate_choice"] = selected_card_id

    st.radio(
        "Select the card you actually want to track",
        options=candidate_ids,
        key="candidate_choice",
        format_func=lambda candidate_id: candidate_map[candidate_id]["label"],
        on_change=queue_candidate_selection,
    )


def render_ebay_table(listings: list[dict[str, Any]]) -> None:
    if not listings:
        st.info("No matching eBay UK listings were returned for this query.")
        return

    dataframe = pd.DataFrame(listings)
    display_df = dataframe[["Title", "Price", "Shipping", "Total", "Listing URL"]]

    column_config: dict[str, Any] = {
        "Title": st.column_config.TextColumn("Title", width="large"),
        "Price": st.column_config.NumberColumn("Price (GBP)", format="%.2f"),
        "Shipping": st.column_config.NumberColumn("Shipping (GBP)", format="%.2f"),
        "Total": st.column_config.NumberColumn("Total (GBP)", format="%.2f"),
    }
    if hasattr(st.column_config, "LinkColumn"):
        column_config["Listing URL"] = st.column_config.LinkColumn(
            "Listing URL",
            display_text="Open listing",
        )

    st.dataframe(
        display_df,
        hide_index=True,
        use_container_width=True,
        column_config=column_config,
    )


def render_source_warnings(warnings: list[str]) -> None:
    for warning in warnings:
        st.warning(warning)


def validate_inputs(card_name: str, card_number: str) -> None:
    if not card_name.strip():
        raise AppError("Enter a card name before running a search.")
    if not card_number.strip():
        raise AppError("Enter a card number before running a search.")


def run_search(
    card_name: str,
    card_number: str,
    preferred_card_id: str | None = None,
) -> dict[str, Any]:
    validate_inputs(card_name, card_number)

    warnings: list[str] = []
    ebay_listings: list[dict[str, Any]] = []
    cardmarket_data: dict[str, Any] | None = None
    card_candidates: list[dict[str, Any]] = []
    card_selection_mode = "unavailable"
    card_selection_note = None

    search_query = f"{card_name} {card_number}".strip()
    pokemon_tcg_api_key = get_optional_env("POKEMON_TCG_API_KEY")
    if pokemon_tcg_api_key:
        try:
            card_lookup = lookup_pokemon_card(
                card_name,
                card_number,
                pokemon_tcg_api_key,
                preferred_card_id=preferred_card_id,
            )
            cardmarket_data = card_lookup["selected_card"]
            card_candidates = card_lookup["candidate_cards"]
            card_selection_mode = card_lookup["selection_mode"]
            card_selection_note = card_lookup["selection_note"]
            search_query = f'{cardmarket_data["name"]} {cardmarket_data["number"]}'.strip()
        except (AppError, requests.RequestException) as error:
            warnings.append(f"Cardmarket lookup unavailable: {error}")
    else:
        warnings.append(
            "Cardmarket lookup skipped: add `POKEMON_TCG_API_KEY` to `.env` to enable Pokemon TCG pricing."
        )

    ebay_client_id = get_optional_env("EBAY_CLIENT_ID")
    ebay_client_secret = get_optional_env("EBAY_CLIENT_SECRET")
    if ebay_client_id and ebay_client_secret:
        try:
            access_token = get_ebay_access_token(ebay_client_id, ebay_client_secret)
            ebay_listings = search_ebay_listings(search_query, access_token)
        except (AppError, requests.RequestException) as error:
            warnings.append(f"eBay search unavailable: {error}")
    else:
        warnings.append(
            "eBay search skipped: add `EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET` to `.env` to enable UK listings."
        )

    if not ebay_listings and not cardmarket_data:
        warning_text = " ".join(warnings)
        raise AppError(f"No data could be loaded for this search. {warning_text}".strip())

    ebay_average = compute_average_total_price(ebay_listings)

    log_search(
        card_name=card_name,
        card_number=card_number,
        ebay_avg_price=ebay_average,
        cardmarket_trend_price=(
            cardmarket_data["trend_price"] if cardmarket_data else None
        ),
        cardmarket_average_sell_price=(
            cardmarket_data["average_sell_price"] if cardmarket_data else None
        ),
        cardmarket_low_price=(cardmarket_data["low_price"] if cardmarket_data else None),
    )

    return {
        "query": {
            "card_name": card_name,
            "card_number": card_number,
            "search_query": search_query,
        },
        "ebay_listings": ebay_listings,
        "ebay_average": ebay_average,
        "cardmarket_data": cardmarket_data,
        "card_candidates": card_candidates,
        "selected_card_id": cardmarket_data["id"] if cardmarket_data else None,
        "card_selection_mode": card_selection_mode,
        "card_selection_note": card_selection_note,
        "show_candidate_options": card_lookup["show_candidate_options"] if cardmarket_data else False,
        "warnings": warnings,
    }


def main() -> None:
    load_env_file()
    init_db()

    st.set_page_config(
        page_title="Pokemon Card Price Tracker",
        layout="wide",
    )

    st.title("Pokemon Card Price Tracker")
    st.write(
        "Search a card by name and number to compare Pokemon TCG Cardmarket prices "
        "with current eBay UK listings."
    )

    st.session_state.setdefault("card_name_input", "")
    st.session_state.setdefault("card_number_input", "")
    st.session_state.setdefault("run_search", False)
    st.session_state.setdefault("pending_history_search", None)
    st.session_state.setdefault("preferred_card_id", None)

    pending_history_search = st.session_state.pop("pending_history_search", None)
    if pending_history_search:
        st.session_state["card_name_input"] = pending_history_search["card_name"]
        st.session_state["card_number_input"] = pending_history_search["card_number"]
        st.session_state["preferred_card_id"] = None
        st.session_state.pop("candidate_choice", None)
        st.session_state["run_search"] = True

    history = fetch_history()
    render_sidebar(history)

    left_col, right_col = st.columns([2, 1])
    with left_col:
        st.text_input(
            "Card Name",
            key="card_name_input",
            placeholder="e.g. Charizard",
        )
    with right_col:
        st.text_input(
            "Card Number",
            key="card_number_input",
            placeholder="e.g. 4/102",
        )

    if st.button("Search", type="primary"):
        st.session_state["preferred_card_id"] = None
        st.session_state.pop("candidate_choice", None)
        st.session_state["run_search"] = True

    if st.session_state.pop("run_search", False):
        card_name = st.session_state.get("card_name_input", "").strip()
        card_number = st.session_state.get("card_number_input", "").strip()
        preferred_card_id = st.session_state.get("preferred_card_id")

        try:
            with st.spinner("Fetching eBay and Cardmarket data..."):
                st.session_state["latest_result"] = run_search(
                    card_name,
                    card_number,
                    preferred_card_id=preferred_card_id,
                )
        except AppError as error:
            st.session_state.pop("latest_result", None)
            st.error(str(error))
        except requests.RequestException as error:
            st.session_state.pop("latest_result", None)
            st.error(f"Network error while calling an external API: {error}")
        except Exception as error:
            st.session_state.pop("latest_result", None)
            st.error(f"Unexpected error: {error}")

    result = st.session_state.get("latest_result")
    if not result:
        st.info("Run a search to view Cardmarket pricing and eBay UK listings.")
        return

    render_source_warnings(result.get("warnings", []))

    card_data = result["cardmarket_data"]
    ebay_listings = result["ebay_listings"]
    ebay_average = result["ebay_average"]
    query = result["query"]
    render_card_candidates(result)

    if card_data:
        st.subheader(f'{card_data["name"]} #{card_data["number"]}')
        render_metrics(card_data, ebay_average)
    else:
        st.subheader(f'{query["card_name"]} #{query["card_number"]}')
        st.info("No Cardmarket data is available for this search.")

    st.subheader("Top 5 eBay UK Listings")
    render_ebay_table(ebay_listings)


if __name__ == "__main__":
    main()
