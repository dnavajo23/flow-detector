"""
data/notifier.py
=================

Envía las señales de `flow_detector.py` por Telegram y/o email. No ejecuta
ninguna orden — es solo el canal de aviso para que decidas manualmente si
entras en la posición.

Variables de entorno (.env):
    TELEGRAM_BOT_TOKEN=...      # crea un bot con @BotFather
    TELEGRAM_CHAT_ID=...        # tu chat_id (@userinfobot te lo da)

    SMTP_HOST=smtp.gmail.com
    SMTP_PORT=587
    SMTP_USER=tu_correo@gmail.com
    SMTP_PASSWORD=tu_app_password        # no la contraseña normal de Gmail
    ALERT_EMAIL_TO=donde_quieres_recibir@correo.com

Puedes configurar solo uno de los dos canales, o ambos. Si no configuras
ninguno, el notifier registra un warning y sigue funcionando (no rompe el
pipeline) para que puedas probar el detector sin tener aún las credenciales.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.mime.text import MIMEText
from typing import Optional

import requests

logger = logging.getLogger("notifier")

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


class AlertNotifier:
    def __init__(
        self,
        telegram_token: Optional[str] = None,
        telegram_chat_id: Optional[str] = None,
        smtp_host: Optional[str] = None,
        smtp_port: Optional[int] = None,
        smtp_user: Optional[str] = None,
        smtp_password: Optional[str] = None,
        email_to: Optional[str] = None,
    ) -> None:
        self.telegram_token = telegram_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id = telegram_chat_id or os.getenv("TELEGRAM_CHAT_ID")
        self.smtp_host = smtp_host or os.getenv("SMTP_HOST")
        self.smtp_port = int(smtp_port or os.getenv("SMTP_PORT") or 587) 
        self.smtp_user = smtp_user or os.getenv("SMTP_USER")
        self.smtp_password = smtp_password or os.getenv("SMTP_PASSWORD")
        self.email_to = email_to or os.getenv("ALERT_EMAIL_TO")
        self.email_from = self.smtp_user

        self.telegram_enabled = bool(self.telegram_token and self.telegram_chat_id)
        self.email_enabled = bool(
            self.smtp_host and self.smtp_user and self.smtp_password and self.email_to
        )
        if not self.telegram_enabled and not self.email_enabled:
            logger.warning(
                "Notifier sin canales configurados (revisa TELEGRAM_* / SMTP_* en .env). "
                "Las señales se registrarán en el log pero no se enviará ninguna alerta real."
            )

    # -- canales individuales -------------------------------------------------
    def send_telegram(self, text: str) -> bool:
        if not self.telegram_enabled:
            return False
        url = TELEGRAM_API.format(token=self.telegram_token)
        try:
            resp = requests.post(
                url,
                data={"chat_id": self.telegram_chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=10,
            )
            resp.raise_for_status()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Error enviando Telegram: %s", exc)
            return False

    def send_email(self, subject: str, body: str) -> bool:
        if not self.email_enabled:
            return False
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = self.email_from
        msg["To"] = self.email_to
        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=15) as server:
                server.starttls()
                server.login(self.smtp_user, self.smtp_password)
                server.sendmail(self.email_from, [self.email_to], msg.as_string())
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Error enviando email: %s", exc)
            return False

    # -- formateo y envío conjunto ---------------------------------------------
    @staticmethod
    def _format(signal, candidate, score: Optional[float] = None, rank: Optional[str] = None) -> tuple[str, str]:
        arrow = "🟢 BULLISH" if signal.direction == "bullish" else "🔴 BEARISH"
        header = f"{arrow} — {signal.underlying}"
        if score is not None:
            header += f"  (score {score}/100)"
        if rank:
            header += f"  [{rank}]"
        lines = [
            header,
            "",
            f"Catalizador: {signal.catalyst.event_type} el {signal.catalyst.event_date}"
            + (f" ({signal.catalyst.description})" if signal.catalyst.description else ""),
            f"Contrato que disparó la señal: {signal.triggering_contract}",
            f"Vol/OI: {signal.vol_oi_ratio:.2f}x",
            f"Prima del contrato hoy: ${signal.contract_day_premium:,.0f}",
            f"Prima calls/puts del subyacente hoy: ${signal.call_premium_underlying:,.0f} / "
            f"${signal.put_premium_underlying:,.0f}",
        ]
        if candidate:
            lines += [
                "",
                "Contrato sugerido:",
                f"  {candidate.option_symbol}",
                f"  strike {candidate.strike} · vence {candidate.expiry} · delta "
                f"{candidate.delta:.2f}" if candidate.delta is not None else "  delta n/d",
                f"  {candidate.reason}",
            ]
        else:
            lines += ["", "(sin contrato adecuado en la cadena actual — revisa manualmente)"]
        body = "\n".join(lines)
        subject = f"[Flow] {arrow.split()[1]} {signal.underlying}"
        if score is not None:
            subject += f" (score {score})"
        return subject, body

    def notify(self, signal, candidate=None, score: Optional[float] = None, rank: Optional[str] = None) -> None:
        subject, body = self._format(signal, candidate, score=score, rank=rank)
        sent_tg = self.send_telegram(body)
        sent_em = self.send_email(subject, body)
        if sent_tg:
            logger.info("Alerta enviada por Telegram (%s)", signal.underlying)
        if sent_em:
            logger.info("Alerta enviada por email (%s)", signal.underlying)
        if not sent_tg and not sent_em:
            logger.warning("Señal generada pero NO enviada por ningún canal:\n%s", body)
