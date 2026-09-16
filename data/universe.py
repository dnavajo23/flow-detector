"""
data/universe.py
==================

Universo de tickers a vigilar, en vez de una lista fija a mano. Trae los
constituyentes actuales de S&P 500 y Nasdaq 100 desde Wikipedia (se
actualiza sola cuando cambian los índices) y los cachea en disco para no
volver a scrapear en cada pasada del cron (la composición de los índices
cambia pocas veces al año).

Nota de coste: pasar 600 tickers a `FlowDetector.poll()` NO significa 600
llamadas caras a la cadena de opciones — `poll()` ya descarta primero
cualquier ticker sin catalizador próximo (ver flow_detector.py), así que
en la práctica solo se consulta la cadena de los pocos tickers que
reportan pronto.

Requisitos:
    pip install pandas lxml requests

Uso:
    from universe import get_universe
    tickers = get_universe({"sp500", "nasdaq100"}, cache_path="universe_cache.json")
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
NASDAQ100_URL = "https://en.wikipedia.org/wiki/Nasdaq-100"

# Wikipedia devuelve 403 Forbidden a peticiones sin un User-Agent que
# parezca un navegador real — algo muy común desde IPs de datacenter como
# las de los runners de GitHub Actions. pd.read_html(url) usaría urllib
# con su User-Agent por defecto y fallaría; en su lugar, pedimos el HTML
# nosotros mismos con este header y se lo pasamos ya descargado a pandas.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

DEFAULT_CACHE_MAX_AGE_DAYS = 7  # los índices no cambian a diario


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
    """Scrapea la tabla de constituyentes del S&P 500 desde Wikipedia."""
    html = _fetch_html(SP500_URL)
    tables = pd.read_html(io.StringIO(html))
    for df in tables:
        col = _find_ticker_column(df)
        if col is not None and len(df) > 400:  # la tabla de constituyentes tiene ~500 filas
            return _clean_tickers(df[col])
    raise RuntimeError("No se encontró la tabla de constituyentes del S&P 500 en Wikipedia.")


def fetch_nasdaq100() -> List[str]:
    """Scrapea la tabla de constituyentes del Nasdaq-100 desde Wikipedia."""
    html = _fetch_html(NASDAQ100_URL)
    tables = pd.read_html(io.StringIO(html))
    for df in tables:
        col = _find_ticker_column(df)
        if col is not None and 80 < len(df) < 150:  # la tabla de componentes tiene ~100 filas
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
    """
    Devuelve la unión de tickers de los índices pedidos (subset de
    {"sp500", "nasdaq100"}). Usa una cache en disco para no scrapear
    Wikipedia en cada ejecución del cron; solo refresca si la cache tiene
    más de `max_age_days` o no existe.
    """
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
