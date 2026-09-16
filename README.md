# Flow Detector — automatizado en GitHub Actions

Detecta volumen inusual en opciones antes de earnings/M&A/regulatorios y te
avisa por Telegram/email. Corre solo, cada 15 minutos durante el horario de
mercado, sin servidor propio — todo dentro de GitHub Actions (gratis para
repos con uso moderado).

## 1. Crear el repositorio

```bash
git init
git add .
git commit -m "Flow detector inicial"
git branch -M main
git remote add origin https://github.com/TU_USUARIO/TU_REPO.git
git push -u origin main
```

Puede ser un repo **privado** (recomendado, ya que subirás código de tu
estrategia) — GitHub Actions funciona igual en repos privados y gratuitos
para uso personal moderado (2.000 minutos/mes en el plan Free).

## 2. Copiar los módulos que ya tienes

Copia a `data/`:
- `polygon_client.py` (no es necesario para el cron, pero sí si luego quieres
  correrlo también en modo tiempo real desde un servidor propio)
- `events_calendar.py` (sí lo usa `flow_check.py` para earnings automáticos)

Borra `data/README_PENDIENTE.md` cuando lo hayas hecho.

## 3. Configurar los "Secrets" (nunca pongas API keys en el código)

En GitHub: **Settings → Secrets and variables → Actions → New repository secret**

Obligatorio:
- `POLYGON_API_KEY`

Recomendado (earnings automáticos):
- `FINNHUB_API_KEY`

Para las alertas (configura al menos uno de los dos):
- `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`
  → crea el bot hablando con **@BotFather** en Telegram (`/newbot`), y
  consigue tu chat_id hablando con **@userinfobot**.
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `ALERT_EMAIL_TO`
  → si usas Gmail, `SMTP_PASSWORD` debe ser un "App Password", no tu
  contraseña normal (actívalo en la configuración de seguridad de tu
  cuenta de Google).

## 4. Universo de tickers y priorización

Ya no hace falta adivinar qué tickers vigilar a mano. El workflow corre con
`--universe both`, que vigila **todo el S&P 500 + Nasdaq 100** — la cache
(`state/universe_cache.json`) evita volver a scrapear Wikipedia en cada
pasada (solo se refresca cada 7 días, o cuando cambie la composición de
los índices).

**Importante sobre el coste de API**: aunque el universo tiene ~600
tickers, `flow_check.py` primero cruza esa lista contra el calendario de
catalizadores (earnings/M&A/regulatorio) y **solo consulta la cadena de
opciones de los tickers que de verdad tienen algo próximo** — normalmente
un puñado por día, no 600. Sin catalizador, no puede haber señal, así que
no tiene sentido gastar ahí la llamada.

Si prefieres seguir con una lista fija tuya, cambia en el workflow:
```yaml
--universe both
```
por:
```yaml
--tickers AAPL,TSLA,NVDA,SPY
```

**Cuando salen varias señales el mismo día**, cada alerta incluye un
**score de 0 a 100** que combina: qué tan por encima del umbral está el
Vol/OI, la prima en dólares (escala logarítmica), qué tan unilateral es
el flujo (todo calls o todo puts pesa más que 50/50), y qué tan cerca
está el catalizador. Las alertas de una misma pasada llegan ordenadas de
mayor a menor score, con una etiqueta `[1/3 de esta pasada]` para que
sepas cuál es la más fuerte sin tener que compararlas tú.

## 5. Ajustar catalizadores manuales

Earnings se cargan solos si configuraste `FINNHUB_API_KEY`. Para M&A o
anuncios regulatorios (no hay proveedor automático todavía), añade filas
a `catalysts.csv`:
```
ticker,event_date,event_type,description
XYZ,2026-10-02,m&a,Rumor de adquisición por ACME Corp
```

## 6. Probarlo

Ve a la pestaña **Actions** de tu repo → workflow **"Flow Check"** →
**"Run workflow"** (botón `workflow_dispatch`). Corre inmediatamente sin
esperar al cron, y puedes ver el log en tiempo real ahí mismo.

Si quieres probar la lógica sin gastar llamadas a la API real:
```bash
python data/flow_check.py --check   # solo valida la conexión
```

## 7. Ya queda solo

Una vez el push esté hecho y los secrets configurados, el workflow
`flow-check.yml` se dispara solo cada 15 minutos, de lunes a viernes,
durante horario de mercado (el propio script comprueba festivos de NYSE y
sale sin hacer nada si el mercado está cerrado). El workflow `keepalive.yml`
evita que GitHub lo desactive por inactividad — no necesitas tocar nada.

## Cómo funciona por dentro

```
.github/workflows/
  flow-check.yml   ← dispara data/flow_check.py cada 15 min (horario mercado)
  keepalive.yml    ← evita que GitHub apague el cron por 60 días de inactividad
data/
  flow_detector.py ← lógica de detección + score + selección de contrato
  flow_check.py    ← script de una sola pasada (sondeo REST, sin WebSocket)
  universe.py      ← S&P 500 + Nasdaq 100 desde Wikipedia, con cache
  notifier.py      ← Telegram / email
  events_calendar.py, polygon_client.py  ← los que ya tenías (opcionales aquí)
catalysts.csv       ← M&A / regulatorios a mano
state/
  flow_state.json      ← dedup de señales entre ejecuciones
  universe_cache.json  ← evita re-scrapear Wikipedia cada 15 min
```

**Importante — límite que decidimos al diseñar esto**: en vez del stream
en vivo por WebSocket (que no encaja con el modelo de ejecución de GitHub
Actions: jobs de máx. 6h y arranque no siempre puntual), `flow_check.py`
sondea la cadena de opciones vía REST cada 15 minutos y usa el volumen
acumulado del día que ya trae Polygon en cada snapshot. Para esta
estrategia (catalizadores a días vista, no señales de segundos) es
suficiente resolución, y es mucho más robusto que depender de una conexión
abierta 6.5 horas seguidas.

Si en el futuro quieres bajar la latencia a segundos (para detectar
sweeps/bloques en tiempo real), necesitarías mover `orchestrator.py`
(el que usa el WebSocket) a un servidor propio siempre encendido (un VPS
de ~$5/mes) — GitHub Actions no es el sitio para eso, aunque puede seguir
guardando el código y desplegándolo ahí automáticamente en cada push.
