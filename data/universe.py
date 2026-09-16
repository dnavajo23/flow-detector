"""
data/universe.py
==================

Universo de tickers a vigilar, en vez de una lista fija a mano. Trae los
constituyentes actuales de S&P 500 y Nasdaq 100 desde Wikipedia (se
actualiza sola cuando cambian los índices) y los cachea en disco para no
volver a scrapear en cada pasada del cron (la composición de los índices
cambia pocas veces al año).

Requisitos:
    pip install pandas lxml requests
"""

from __future__ import annotations

import io
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Optional, Set

import pandas as pd
import requests

logger = logging.getLogger("universe")

SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
NASDAQ100_URL = "https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

DEFAULT_CACHE_MAX_AGE_DAYS = 7


def _fetch_html(url: str) -> str:
    resp = requests.get(url, headers=_HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.text


def _find_ticker_column(df: pd.DataFrame) -> Optional[str]:
    for col in df.columns:
        if str(col).strip().lower() in ("symbol", "ticker", "ticker symbol"):
            return col
    return None


def _clean_tickers(series: pd.Series) -> List[str]:
    return sorted(
        {
            str(t).strip().upper()
            for t in series.tolist()
            if isinstance(t, str) and t.strip()
        }
    )


def fetch_sp500() -> List[str]:
    html = _fetch_html(SP500_URL)
    tables = pd.read_html(io.StringIO(html))
    for df in tables:
        col = _find_ticker_column(df)
        if col is not None and len(df) > 400:
            return _clean_tickers(df[col])
    raise RuntimeError("No se encontró la tabla de constituyentes del S&P 500 en Wikipedia.")


def fetch_nasdaq100() -> List[str]:
    html = _fetch_html(NASDAQ100_URL)
    tables = pd.read_html(io.StringIO(html))
    for df in tables:
        col = _find_ticker_column(df)
        if col is not None and 80 < len(df) < 150:
            return _clean_tickers(df[col])
    raise RuntimeError("No se encontró la tabla de constituyentes del Nasdaq-100 en Wikipedia.")


FETCHERS = {
    "sp500": fetch_sp500,
    "nasdaq100": fetch_nasdaq100,
}


def get_universe(
    indices: Iterable[str],
    cache_path: Optional[str] = None,
    max_age_days: int = DEFAULT_CACHE_MAX_AGE_DAYS,
) -> List[str]:
    indices = sorted(set(i.lower() for i in indices))
    cache = Path(cache_path) if cache_path else None

    if cache and cache.exists():
        try:
            data = json.loads(cache.read_text())
            cached_at = datetime.fromisoformat(data["cached_at"])
            if data.get("indices") == indices and datetime.now() - cached_at < timedelta(days=max_age_days):
                logger.info("Universo cargado de cache (%s), %d tickers.", cache, len(data["tickers"]))
                return data["tickers"]
        except (json.JSONDecodeError, KeyError, ValueError, OSError) as exc:
            logger.warning("Cache de universo corrupta (%s), se regenera: %s", cache, exc)

    tickers: Set[str] = set()
    for idx in indices:
        fetcher = FETCHERS.get(idx)
        if fetcher is None:
            raise ValueError(f"Índice desconocido: {idx!r} (usa 'sp500' y/o 'nasdaq100')")
        fetched = fetcher()
        logger.info("%s: %d tickers obtenidos de Wikipedia.", idx, len(fetched))
        tickers.update(fetched)

    result = sorted(tickers)

    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps({"indices": indices, "cached_at": datetime.now().isoformat(), "tickers": result})
        )

    return result


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    idxs = sys.argv[1:] or ["sp500", "nasdaq100"]
    all_tickers = get_universe(idxs)
    print(f"{len(all_tickers)} tickers: {', '.join(all_tickers[:20])}...")
