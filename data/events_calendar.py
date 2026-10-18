"""
data/events_calendar.py
========================

Proveedor de fechas de earnings usando la API de Finnhub.
Devuelve objetos EarningsEvent compatibles con flow_detector.catalyst_from_earnings_event.

Variables de entorno:
    FINNHUB_API_KEY=tu_api_key   (obtenla gratis en finnhub.io)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date
from typing import List

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("events_calendar")

FINNHUB_BASE = "https://finnhub.io/api/v1"


@dataclass(slots=True)
class EarningsEvent:
    ticker: str
    report_date: date
    fiscal_period: str | None = None
    fiscal_year: int | None = None
    source: str = "finnhub"


class FinnhubEarningsProvider:
    """
    Consulta el calendario de earnings de Finnhub para un rango de fechas
    y filtra solo los tickers que están en la lista de vigilancia.

    Usa el endpoint /calendar/earnings (no gasta llamadas por ticker,
    trae todo el mercado en una sola petición) y luego filtra por la
    lista de tickers que pasas a fetch().
    """

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.getenv("FINNHUB_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "Falta FINNHUB_API_KEY en el entorno. "
                "Consíguela gratis en https://finnhub.io/register"
            )

    def fetch(
        self,
        tickers: List[str],
        start: date,
        end: date,
    ) -> List[EarningsEvent]:
        """
        Devuelve los EarningsEvent de los tickers vigilados que reportan
        entre start y end (inclusive).
        """
        ticker_set = {t.upper() for t in tickers}
        url = f"{FINNHUB_BASE}/calendar/earnings"
        params = {
            "from": start.isoformat(),
            "to": end.isoformat(),
            "token": self.api_key,
        }
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.error("Error consultando Finnhub earnings calendar: %s", exc)
            return []

        events: List[EarningsEvent] = []
        for entry in data.get("earningsCalendar", []):
            symbol = (entry.get("symbol") or "").upper()
            if symbol not in ticker_set:
                continue
            raw_date = entry.get("date")
            if not raw_date:
                continue
            try:
                report_date = date.fromisoformat(raw_date)
            except ValueError:
                continue

            quarter = entry.get("quarter")
            year = entry.get("year")
            fiscal_period = f"Q{quarter}" if quarter else None

            events.append(
                EarningsEvent(
                    ticker=symbol,
                    report_date=report_date,
                    fiscal_period=fiscal_period,
                    fiscal_year=int(year) if year else None,
                    source="finnhub",
                )
            )

        logger.info(
            "Finnhub earnings: %d eventos entre %s y %s para los tickers vigilados.",
            len(events),
            start,
            end,
        )
        return events
