# PokemonCardChecker
Checks the prices of pokemon cards

Local Streamlit app for checking Pokemon card prices using:

- eBay Browse API for live UK offer prices
- Pokemon TCG API (`pokemontcg.io`) for Cardmarket pricing data
- SQLite for local search history

## What It Does

- Search by card name and card number
- Find the closest Cardmarket match when the exact number does not line up
- Let you choose the correct card when multiple matches are plausible
- Show Cardmarket pricing in GBP
- Show eBay UK offer summaries in GBP
- Display the selected card image
- Save search history locally in `history.db`

## Requirements

- Python 3.11 or newer
- eBay credentials if you want eBay UK prices
- Pokemon TCG API key for Cardmarket data

## Setup

Clone the repo and move into the project directory:

```bash
git clone https://github.com/Opperdraak/PokemonCardChecker.git
cd PokemonCardChecker
```

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

## Environment Variables

Create a local `.env` file in the project root.

You can copy the example file:

```bash
cp .env.example .env
```

Example:

```env
# eBay is optional. Leave these blank until you have real eBay Browse API credentials.
EBAY_CLIENT_ID=
EBAY_CLIENT_SECRET=
POKEMON_TCG_API_KEY=b21058ac-9b94-4ee6-b51a-b10a7002aa4c
```

Notes:

- `.env` is local-only and should not be committed
- eBay is optional
- if eBay credentials are empty, the app still works for Cardmarket

## Run The App

```bash
streamlit run app.py
```

Then open:

- [http://localhost:8501](http://localhost:8501)

## Data Sources

- eBay Browse API for UK listings and offer pricing
- Pokemon TCG API for card metadata and Cardmarket pricing
- ECB daily reference rates for EUR to GBP conversion

## Current Behavior

- Cardmarket prices are converted from EUR to GBP in-app
- eBay prices are shown in GBP
- GBP values are formatted with `£`
- the app prefers UK eBay listings only
- every search is stored in `history.db`

## Limitations

- The Cardmarket fields exposed through `pokemontcg.io` do not provide full per-condition buckets like `NM`, `LP`, `HP`, and `Mint`
- eBay data depends on valid production credentials for the Browse API
- exchange conversion uses the latest ECB reference rate, not a live market trading feed

## Files

- `app.py`: main Streamlit app
- `requirements.txt`: Python dependencies
- `.env.example`: example environment file
- `history.db`: local SQLite history database, created automatically
