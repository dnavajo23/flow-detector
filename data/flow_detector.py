"""
data/flow_detector.py
======================

Detector de "unusual options activity" (UOA) previo a catalizadores
corporativos (earnings, M&A, anuncios regulatorios), y selector del
contrato óptimo para posicionarse en la dirección de la señal.

Estrategia que implementa
--------------------------
1. Se acumula, por contrato, el volumen y la prima operados en el día
   (alimentado en tiempo real por el callback `on_trade`, compatible con
   `data.polygon_client.PolygonOptionsClient`).
2. Se cruza ese volumen contra el **Open Interest** (refrescado por REST vía
   `list_snapshot_options_chain`) para obtener el ratio Vol/OI. Un ratio alto
   indica posiciones NUEVAS (no roll ni cierre de las de siempre) — es la
   señal estándar de "unusual activity".
3. Solo se considera señal si el subyacente tiene un catalizador (earnings /
   M&A / regulatorio) dentro de una ventana próxima (`event_window_days`).
4. Se exige una prima mínima en dólares para descartar contratos ilíquidos
   con ratios Vol/OI altísimos pero dinero irrelevante.
5. Se determina el lado dominante (calls vs puts) por prima acumulada del
   día en el subyacente → dirección de la señal (bullish/bearish).
6. `ContractSelector` elige el contrato concreto a comprar: NO clona el
   contrato donde se vio el volumen (puede ser un hedge o parte de un
   spread institucional) — en su lugar busca, dentro de la misma cadena,
   el strike/vencimiento que mejor "captura" el movimiento esperado:
   delta ~0.30-0.50 y vencimiento posterior al catalizador con margen.

Fuentes de catalizadores
------------------------
- Earnings: adapta objetos `EarningsEvent` de `data.events_calendar`
  (proveedor Finnhub/Polygon-Benzinga ya construido).
- M&A / regulatorio: TODAVÍA NO hay un proveedor de datos automático en el
  proyecto. Mientras tanto, se pueden cargar manualmente vía un CSV
  (--catalysts-csv) con columnas: ticker,event_date,event_type,description
  El siguiente módulo natural del proyecto sería un `data/news_events.py`
  que rastree anuncios de M&A/regulatorios (ej. vía un proveedor de noticias
  o filtrando 8-K/13D de EDGAR) y devuelva `CatalystEvent` directamente.

Requisitos:
    pip install polygon-api-client python-dotenv

Variables de entorno (.env):
    POLYGON_API_KEY=tu_api_key

Uso rápido:
    python data/flow_detector.py --check
    python data/flow_detector.py --tickers AAPL,TSLA,NVDA --demo
    python data/flow_detector.py --tickers AAPL,TSLA --catalysts-csv catalysts.csv
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import os
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, List, Literal, Optional

from dotenv import load_dotenv
from polygon import RESTClient

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("flow_detector")

# --------------------------------------------------------------------------- #
#  Parámetros por defecto de la estrategia (todos configurables)
# --------------------------------------------------------------------------- #
DEFAULT_MIN_VOL_OI_RATIO = 2.0       # volumen del día >= 2x el Open Interest
DEFAULT_MIN_PREMIUM = 50_000.0       # descarta contratos irrelevantes en $
DEFAULT_EVENT_WINDOW_DAYS = 5        # catalizador dentro de los próximos N días
DEFAULT_MIN_DAYS_AFTER_EVENT = 3     # el contrato elegido vence >= evento+3d
DEFAULT_MAX_DAYS_AFTER_EVENT = 28    # y <= evento+28d (no correr contra el reloj)
DEFAULT_TARGET_DELTA_RANGE = (0.30, 0.50)

EventType = Literal["earnings", "m&a", "regulatory"]
Direction = Literal["bullish", "bearish"]


# --------------------------------------------------------------------------- #
#  Modelos
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class CatalystEvent:
    """Evento corporativo que puede anticipar un movimiento fuerte."""

    ticker: str
    event_date: date
    event_type: EventType
    description: Optional[str] = None
    source: str = "manual"

    def as_dict(self) -> dict:
        d = asdict(self)
        d["event_date"] = self.event_date.isoformat()
        return d


def catalyst_from_earnings_event(ev) -> CatalystEvent:
    """Adapta un `EarningsEvent` de data.events_calendar a CatalystEvent."""
    period = " ".join(
        str(p) for p in (getattr(ev, "fiscal_period", None), getattr(ev, "fiscal_year", None)) if p
    ) or None
    return CatalystEvent(
        ticker=ev.ticker,
        event_date=ev.report_date,
        event_type="earnings",
        description=period,
        source=getattr(ev, "source", "events_calendar"),
    )


def load_catalysts_csv(path: str) -> List[CatalystEvent]:
    """Carga catalizadores M&A/regulatorios (o earnings) desde un CSV manual.

    Columnas esperadas: ticker,event_date,event_type,description
    event_date en formato YYYY-MM-DD. event_type: earnings|m&a|regulatory
    """
    events: List[CatalystEvent] = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            events.append(
                CatalystEvent(
                    ticker=row["ticker"].strip().upper(),
                    event_date=datetime.strptime(row["event_date"].strip(), "%Y-%m-%d").date(),
                    event_type=row["event_type"].strip().lower(),  # type: ignore[arg-type]
                    description=row.get("description") or None,
                    source="csv",
                )
            )
    logger.info("Cargados %d catalizadores desde %s", len(events), path)
    return events


@dataclass(slots=True)
class ContractStats:
    """Acumulador intradía de un contrato individual."""

    option_symbol: str
    underlying: str
    right: str  # "C" | "P"
    strike: Optional[float]
    expiry: Optional[date]
    day_volume: int = 0
    day_premium: float = 0.0
    open_interest: Optional[float] = None
    delta: Optional[float] = None
    mid_price: Optional[float] = None

    @property
    def vol_oi_ratio(self) -> Optional[float]:
        if not self.open_interest:
            return None
        return self.day_volume / self.open_interest


@dataclass(slots=True)
class FlowSignal:
    """Señal emitida cuando un contrato cumple todos los filtros."""

    underlying: str
    direction: Direction
    triggering_contract: str
    vol_oi_ratio: float
    contract_day_premium: float
    call_premium_underlying: float
    put_premium_underlying: float
    catalyst: CatalystEvent
    timestamp: datetime

    def as_dict(self) -> dict:
        d = asdict(self)
        d["catalyst"] = self.catalyst.as_dict()
        d["timestamp"] = self.timestamp.isoformat()
        return d


@dataclass(slots=True)
class TradeCandidate:
    """Contrato elegido por ContractSelector para capturar el movimiento."""

    underlying: str
    option_symbol: str
    right: str
    strike: float
    expiry: date
    delta: Optional[float]
    open_interest: Optional[float]
    mid_price: Optional[float]
    reason: str

    def as_dict(self) -> dict:
        d = asdict(self)
        d["expiry"] = self.expiry.isoformat()
        return d


# --------------------------------------------------------------------------- #
#  Acceso a la cadena de opciones (OI, delta, precio) vía REST
# --------------------------------------------------------------------------- #
class OptionsChainSnapshot:
    """Envuelve `list_snapshot_options_chain` para refrescar OI/delta/precio."""

    def __init__(self, client: RESTClient) -> None:
        self.client = client

    def fetch(self, underlying: str) -> List[ContractStats]:
        """Trae la cadena completa de opciones vigentes de un subyacente."""
        out: List[ContractStats] = []
        try:
            for c in self.client.list_snapshot_options_chain(underlying):
                details = c.details
                greeks = c.greeks
                day = c.day
                quote = c.last_quote
                if details is None or details.ticker is None:
                    continue
                mid = None
                if quote and quote.bid is not None and quote.ask is not None:
                    mid = (quote.bid + quote.ask) / 2
                out.append(
                    ContractStats(
                        option_symbol=details.ticker,
                        underlying=underlying,
                        right="C" if (details.contract_type or "").lower() == "call" else "P",
                        strike=details.strike_price,
                        expiry=(
                            datetime.strptime(details.expiration_date, "%Y-%m-%d").date()
                            if details.expiration_date
                            else None
                        ),
                        day_volume=int(day.volume) if day and day.volume else 0,
                        open_interest=c.open_interest,
                        delta=greeks.delta if greeks else None,
                        mid_price=mid,
                    )
                )
                # Estima la prima acumulada del día sin necesitar el stream en
                # vivo: volumen del día x precio medio del día x 100 (tamaño
                # de contrato). Se usa vwap si está disponible, si no el
                # último close conocido.
                ref_price = None
                if day is not None:
                    ref_price = day.vwap or day.close
                if ref_price and out[-1].day_volume:
                    out[-1].day_premium = out[-1].day_volume * ref_price * 100
        except Exception as exc:  # noqa: BLE001 - se registra y se propaga vacío
            logger.error("Error obteniendo snapshot de %s: %s", underlying, exc)
        return out


# --------------------------------------------------------------------------- #
#  Detector de flow
# --------------------------------------------------------------------------- #
class FlowDetector:
    """
    Acumula trades en tiempo real (vía `on_trade`), los cruza contra OI y
    catalizadores próximos, y emite `FlowSignal` cuando se cumplen los
    umbrales de la estrategia.

    Parámetros
    ----------
    on_signal : callable
        Recibe cada `FlowSignal` disparada. Ahí enchufas la ejecución real
        (o el paso a `ContractSelector` para elegir el contrato a comprar).
    min_vol_oi_ratio, min_premium, event_window_days : ver constantes DEFAULT_*.
    """

    def __init__(
        self,
        chain: OptionsChainSnapshot,
        on_signal: Optional[Callable[[FlowSignal], None]] = None,
        min_vol_oi_ratio: float = DEFAULT_MIN_VOL_OI_RATIO,
        min_premium: float = DEFAULT_MIN_PREMIUM,
        event_window_days: int = DEFAULT_EVENT_WINDOW_DAYS,
    ) -> None:
        self.chain = chain
        self.on_signal = on_signal or (lambda s: logger.info("SEÑAL: %s", s.as_dict()))
        self.min_vol_oi_ratio = min_vol_oi_ratio
        self.min_premium = min_premium
        self.event_window_days = event_window_days

        self._contracts: Dict[str, ContractStats] = {}
        self._call_premium: Dict[str, float] = defaultdict(float)
        self._put_premium: Dict[str, float] = defaultdict(float)
        self._catalysts: Dict[str, List[CatalystEvent]] = defaultdict(list)
        self._already_signaled: set[str] = set()
        self.last_signals: List[FlowSignal] = []

    # -- catalizadores -----------------------------------------------------
    def register_catalysts(self, events: Iterable[CatalystEvent]) -> None:
        for ev in events:
            self._catalysts[ev.ticker.upper()].append(ev)
        logger.info("Catalizadores registrados para %d tickers", len(self._catalysts))

    def _upcoming_catalyst(self, underlying: str, today: date) -> Optional[CatalystEvent]:
        candidates = [
            ev
            for ev in self._catalysts.get(underlying.upper(), [])
            if today <= ev.event_date <= today + timedelta(days=self.event_window_days)
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda e: e.event_date)  # el más próximo

    # -- refresco de Open Interest ------------------------------------------
    def refresh_open_interest(self, underlyings: Iterable[str]) -> None:
        """OI se actualiza ~1 vez al día; llamar antes de sesión (o --check)."""
        for u in underlyings:
            for stats in self.chain.fetch(u):
                existing = self._contracts.get(stats.option_symbol)
                if existing:
                    existing.open_interest = stats.open_interest
                    existing.delta = stats.delta
                    existing.mid_price = stats.mid_price
                    existing.strike = existing.strike or stats.strike
                    existing.expiry = existing.expiry or stats.expiry
                else:
                    stats.day_volume = 0  # el volumen en vivo lo acumula on_trade
                    self._contracts[stats.option_symbol] = stats
        logger.info("Open Interest refrescado para %d contratos", len(self._contracts))

    # -- ingestión por sondeo (sin WebSocket, apta para cron) -----------------
    def poll(self, underlyings: Iterable[str], today: Optional[date] = None) -> List[FlowSignal]:
        """
        Alternativa a `on_trade` para entornos sin conexión persistente
        (p.ej. un job programado de GitHub Actions): en cada llamada trae la
        cadena de opciones completa vía REST, recalcula volumen/OI/prima
        estimada de cada contrato y reevalúa las señales. No requiere el
        WebSocket de polygon_client.

        IMPORTANTE (coste de API): antes de tocar la cadena de opciones,
        se descarta cualquier ticker sin catalizador en la ventana
        configurada — sin catalizador no puede haber señal (ver
        `_upcoming_catalyst`), así que no tiene sentido gastar una llamada
        cara de `list_snapshot_options_chain` en él. Esto es lo que hace
        viable vigilar un universo grande (S&P 500 + Nasdaq 100) en vez de
        una lista corta: de cientos de tickers, normalmente solo se
        consulta la cadena de un puñado (los que reportan pronto).

        Devuelve la lista de señales nuevas disparadas en esta pasada
        (además de invocar `on_signal` para cada una, igual que siempre).
        """
        today = today or date.today()
        underlyings = list(underlyings)
        self.last_signals = []

        candidates = [u for u in underlyings if self._upcoming_catalyst(u, today) is not None]
        logger.info(
            "%d/%d tickers con catalizador próximo -> consultando solo esos.",
            len(candidates),
            len(underlyings),
        )

        for underlying in candidates:
            self._call_premium[underlying.upper()] = 0.0
            self._put_premium[underlying.upper()] = 0.0
            for stats in self.chain.fetch(underlying):
                self._contracts[stats.option_symbol] = stats
                if stats.right == "C":
                    self._call_premium[underlying.upper()] += stats.day_premium
                elif stats.right == "P":
                    self._put_premium[underlying.upper()] += stats.day_premium

            for stats in list(self._contracts.values()):
                if stats.underlying.upper() == underlying.upper():
                    self._evaluate(stats, today=today)

        self.last_signals.sort(key=lambda s: self.score_signal(s), reverse=True)
        return self.last_signals

    def score_signal(self, signal: FlowSignal) -> float:
        """
        Score 0-100 para priorizar cuando salen varias señales el mismo día.
        Combina cuatro factores (cada uno normalizado 0-100):

          - Vol/OI: qué tan por encima del umbral está (capado en 3x el umbral).
          - Prima: escala logarítmica frente al mínimo configurado (el dinero
            real detrás de la señal no crece linealmente en relevancia).
          - Consistencia direccional: qué tan unilateral es el flujo del día
            en calls vs puts del subyacente (100 = todo un lado, 0 = 50/50).
          - Cercanía al catalizador: más peso cuanto más próximo esté el
            evento dentro de la ventana configurada.

        Pesos por defecto: 30% Vol/OI, 30% prima, 20% direccional, 20% cercanía.
        """
        vol_oi_norm = min(signal.vol_oi_ratio / self.min_vol_oi_ratio, 3.0) / 3.0 * 100

        premium_ratio = max(signal.contract_day_premium, 1.0) / max(self.min_premium, 1.0)
        premium_norm = min(math.log10(max(premium_ratio, 1.0)) / math.log10(20), 1.0) * 100

        total_prem = signal.call_premium_underlying + signal.put_premium_underlying
        direction_norm = (
            abs(signal.call_premium_underlying - signal.put_premium_underlying) / total_prem * 100
            if total_prem > 0
            else 0.0
        )

        days_to_event = (signal.catalyst.event_date - signal.timestamp.date()).days
        proximity_norm = (
            max(0.0, (self.event_window_days - days_to_event) / self.event_window_days) * 100
            if self.event_window_days > 0
            else 0.0
        )

        score = 0.30 * vol_oi_norm + 0.30 * premium_norm + 0.20 * direction_norm + 0.20 * proximity_norm
        return round(score, 1)

    # -- ingestión en tiempo real --------------------------------------------
    def on_trade(self, trade) -> None:
        """Callback compatible con `data.polygon_client.PolygonOptionsClient`.

        `trade` debe exponer: option_symbol, underlying, right, strike, expiry,
        size, premium, timestamp (igual que `OptionTrade`).
        """
        stats = self._contracts.get(trade.option_symbol)
        if stats is None:
            stats = ContractStats(
                option_symbol=trade.option_symbol,
                underlying=trade.underlying,
                right=trade.right or "?",
                strike=trade.strike,
                expiry=trade.expiry,
            )
            self._contracts[trade.option_symbol] = stats

        stats.day_volume += trade.size
        stats.day_premium += trade.premium

        underlying = trade.underlying.upper()
        if trade.right == "C":
            self._call_premium[underlying] += trade.premium
        elif trade.right == "P":
            self._put_premium[underlying] += trade.premium

        self._evaluate(stats, today=trade.timestamp.date())

    # -- evaluación de la señal ------------------------------------------------
    def _evaluate(self, stats: ContractStats, today: date) -> None:
        if stats.day_premium < self.min_premium:
            return
        ratio = stats.vol_oi_ratio
        if ratio is None or ratio < self.min_vol_oi_ratio:
            return

        catalyst = self._upcoming_catalyst(stats.underlying, today)
        if catalyst is None:
            return

        # una señal por contrato/día para no inundar de duplicados
        signal_key = f"{stats.option_symbol}:{today.isoformat()}"
        if signal_key in self._already_signaled:
            return

        call_prem = self._call_premium.get(stats.underlying.upper(), 0.0)
        put_prem = self._put_premium.get(stats.underlying.upper(), 0.0)
        direction: Direction = "bullish" if call_prem >= put_prem else "bearish"

        signal = FlowSignal(
            underlying=stats.underlying.upper(),
            direction=direction,
            triggering_contract=stats.option_symbol,
            vol_oi_ratio=ratio,
            contract_day_premium=stats.day_premium,
            call_premium_underlying=call_prem,
            put_premium_underlying=put_prem,
            catalyst=catalyst,
            timestamp=datetime.now(timezone.utc),
        )
        self._already_signaled.add(signal_key)
        self.last_signals.append(signal)
        self.on_signal(signal)


# --------------------------------------------------------------------------- #
#  Selector del contrato a comprar
# --------------------------------------------------------------------------- #
class ContractSelector:
    """
    Dada una `FlowSignal` (dirección + subyacente + catalizador), elige el
    contrato concreto a comprar dentro de la misma cadena de opciones.

    No clona el contrato donde se detectó el volumen: busca, dentro del
    lado correcto (calls si bullish, puts si bearish), el que mejor cumple
    delta objetivo y vencimiento posterior al catalizador.
    """

    def __init__(
        self,
        chain: OptionsChainSnapshot,
        min_days_after_event: int = DEFAULT_MIN_DAYS_AFTER_EVENT,
        max_days_after_event: int = DEFAULT_MAX_DAYS_AFTER_EVENT,
        target_delta_range: tuple[float, float] = DEFAULT_TARGET_DELTA_RANGE,
    ) -> None:
        self.chain = chain
        self.min_days_after_event = min_days_after_event
        self.max_days_after_event = max_days_after_event
        self.target_delta_range = target_delta_range

    def pick(self, signal: FlowSignal) -> Optional[TradeCandidate]:
        wanted_right = "C" if signal.direction == "bullish" else "P"
        window_start = signal.catalyst.event_date + timedelta(days=self.min_days_after_event)
        window_end = signal.catalyst.event_date + timedelta(days=self.max_days_after_event)
        lo, hi = self.target_delta_range

        best: Optional[ContractStats] = None
        best_score = float("inf")
        for c in self.chain.fetch(signal.underlying):
            if c.right != wanted_right or c.expiry is None or c.delta is None:
                continue
            if not (window_start <= c.expiry <= window_end):
                continue
            abs_delta = abs(c.delta)
            if not (lo <= abs_delta <= hi):
                continue
            # más cerca del centro del rango de delta = mejor; a igualdad,
            # prioriza más Open Interest (más líquido)
            score = abs(abs_delta - (lo + hi) / 2) - (c.open_interest or 0) * 1e-9
            if score < best_score:
                best_score = score
                best = c

        if best is None:
            logger.warning(
                "Sin contrato %s de %s con delta en %.2f-%.2f y vencimiento %s..%s",
                "call" if wanted_right == "C" else "put",
                signal.underlying,
                lo,
                hi,
                window_start,
                window_end,
            )
            return None

        return TradeCandidate(
            underlying=signal.underlying,
            option_symbol=best.option_symbol,
            right=best.right,
            strike=best.strike or 0.0,
            expiry=best.expiry,  # type: ignore[arg-type]
            delta=best.delta,
            open_interest=best.open_interest,
            mid_price=best.mid_price,
            reason=(
                f"delta {best.delta:.2f} dentro de {lo}-{hi}, "
                f"vence {best.expiry} (evento {signal.catalyst.event_date} "
                f"+{(best.expiry - signal.catalyst.event_date).days}d)"
            ),
        )


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #
def _make_client() -> RESTClient:
    api_key = os.getenv("POLYGON_API_KEY")
    if not api_key:
        raise SystemExit("Falta POLYGON_API_KEY en el entorno (.env).")
    return RESTClient(api_key)


def _demo(tickers: List[str], detector: FlowDetector, selector: ContractSelector) -> None:
    """Simula trades para poder probar la lógica sin depender del mercado
    abierto ni de datos reales de OI (usa un contrato ficticio por ticker)."""
    from types import SimpleNamespace

    today = date.today()
    event_date = today + timedelta(days=2)
    for t in tickers:
        detector.register_catalysts(
            [CatalystEvent(ticker=t, event_date=event_date, event_type="earnings", source="demo")]
        )
        symbol = f"O:{t}DEMO"
        stats = ContractStats(
            option_symbol=symbol,
            underlying=t,
            right="C",
            strike=100.0,
            expiry=event_date + timedelta(days=14),
            open_interest=200,
        )
        detector._contracts[symbol] = stats  # noqa: SLF001 - demo interno
        trade = SimpleNamespace(
            option_symbol=symbol,
            underlying=t,
            right="C",
            strike=100.0,
            expiry=stats.expiry,
            size=500,  # con OI=200 esto da vol/oi = 2.5x
            premium=120_000.0,
            timestamp=datetime.now(timezone.utc),
        )
        detector.on_trade(trade)


def main() -> None:
    parser = argparse.ArgumentParser(description="Detector de unusual options activity")
    parser.add_argument("--tickers", type=str, default="AAPL,TSLA,NVDA,SPY")
    parser.add_argument("--min-vol-oi", type=float, default=DEFAULT_MIN_VOL_OI_RATIO)
    parser.add_argument("--min-premium", type=float, default=DEFAULT_MIN_PREMIUM)
    parser.add_argument("--event-window-days", type=int, default=DEFAULT_EVENT_WINDOW_DAYS)
    parser.add_argument("--catalysts-csv", type=str, default=None)
    parser.add_argument("--check", action="store_true", help="Solo verifica conexión REST")
    parser.add_argument("--demo", action="store_true", help="Simula trades sin mercado abierto")
    args = parser.parse_args()

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]

    if args.check:
        client = _make_client()
        chain = OptionsChainSnapshot(client)
        sample = chain.fetch(tickers[0])
        print(f"OK: {len(sample)} contratos obtenidos para {tickers[0]} (snapshot REST).")
        return

    def handle_signal(signal: FlowSignal) -> None:
        print("\n=== SEÑAL DE FLOW ===")
        for k, v in signal.as_dict().items():
            print(f"  {k}: {v}")
        candidate = selector.pick(signal)
        if candidate:
            print("--- Contrato sugerido ---")
            for k, v in candidate.as_dict().items():
                print(f"  {k}: {v}")
        else:
            print("  (sin contrato adecuado en la cadena actual)")

    if args.demo:
        # En demo no hace falta API key ni REST real: el chain no se usa
        # para OI (ya viene fijado), pero ContractSelector sí lo necesitaría
        # para elegir contrato real; en modo demo se omite esa parte.
        chain = OptionsChainSnapshot(client=None)  # type: ignore[arg-type]
        detector = FlowDetector(
            chain=chain,
            on_signal=lambda s: print("\n=== SEÑAL DE FLOW (demo) ===\n", s.as_dict()),
            min_vol_oi_ratio=args.min_vol_oi,
            min_premium=args.min_premium,
            event_window_days=args.event_window_days,
        )
        selector = ContractSelector(chain=chain)
        _demo(tickers, detector, selector)
        return

    client = _make_client()
    chain = OptionsChainSnapshot(client)
    detector = FlowDetector(
        chain=chain,
        on_signal=handle_signal,
        min_vol_oi_ratio=args.min_vol_oi,
        min_premium=args.min_premium,
        event_window_days=args.event_window_days,
    )
    selector = ContractSelector(chain=chain)

    if args.catalysts_csv:
        detector.register_catalysts(load_catalysts_csv(args.catalysts_csv))
    else:
        logger.warning(
            "Sin --catalysts-csv: no habrá catalizadores M&A/regulatorios cargados. "
            "Registra earnings con detector.register_catalysts([...]) desde "
            "data.events_calendar, o pasa un CSV con eventos manuales."
        )

    detector.refresh_open_interest(tickers)
    print(
        "Detector listo. Conecta esto al callback on_trade de "
        "PolygonOptionsClient (data/polygon_client.py) para alimentar trades "
        "en tiempo real, p.ej.:\n\n"
        "    from data.polygon_client import PolygonOptionsClient\n"
        "    client = PolygonOptionsClient(tickers, on_trade=detector.on_trade)\n"
        "    client.run()\n"
    )


if __name__ == "__main__":
    main()
