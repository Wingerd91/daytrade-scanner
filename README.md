# Trade-scanner (aandelen + crypto)

Scant de markt en stuurt kansen naar Telegram. **De bot plaatst geen orders** en geeft geen advies: jij beslist altijd zelf. Hij draait gratis op GitHub, ook als je computer uit staat.

## ⚠️ Twee soorten signalen: lees dit eerst

| | ⚡ **DAYTRADE** | 🟣 **SWING** |
|---|---|---|
| **Hoe lang vasthouden** | Vandaag openen én **vandaag sluiten** | **Dagen tot maanden** |
| **Grafiek** | 5-minutencandles | Dag- en weekcandles |
| **Wanneer gescand** | Elke 5 minuten | Eén keer per dag (± 22:15) |
| **Stop** | Vast, staat in het bericht | Vast, staat in het bericht |
| **Doel** | Vast: 2× je risico (2R) | **Jouw red zone** (Fib, zelf tekenen). De bot zegt hoe hoog die minimaal moet liggen voor 1:2 |
| **Niet geraakt?** | Om het eind van de dag sluiten | Blijven houden tot red zone of stop |
| **Slagingspercentage** | Ja (backtest, na kosten) | Nee |
| **Automatische papertrade** | Ja | Nee, zelf bijhouden |

**Gouden regel:** staat er ⚡ bovenaan, dan is de trade vandaag klaar. Staat er 🟣, dan is het een lange trade. Een ⚡-trade die verliest **niet** "even laten staan" als swing: dat is precies hoe accounts leeglopen.

## Wat je in Telegram krijgt

- **⚡ DAYTRADE**: symbool, setup, prijs, stop, doel (2R), aantal, risico, slagingskans na kosten en de kosten in R.
- **✅ / ❌ / ⏱ papertrade gesloten**: doel geraakt, stop geraakt of einde dag, met het resultaat in R na kosten.
- **🟣 SWING**: setup, stop met uitleg, aantal, en de minimale hoogte van je red zone voor 1:2.
- **📋 Dagoverzicht** (± 22:15): signalen van vandaag, gesloten papertrades, wat nog open staat, je voortgang naar 30 papertrades per setup en de swing-signalen.
- **📊 Backtest** (zondag): nieuwe slagingspercentages.
- **❌ Run mislukt**: als er op GitHub iets misgaat, met een link.

## ⚡ Daytrade-setups

| Setup | Long als… | Stop |
|---|---|---|
| ORB | koers breekt boven de eerste 15 minuten, boven VWAP, EMA9 > EMA20 | midden van de opening range |
| Breakout high gisteren | koers gaat door de high van gisteren | 0,5× ATR(5m) onder die high |
| VWAP-pullback | trend omhoog, candle tikt VWAP/EMA20 aan en sluit erboven | onder candle-low/VWAP |

Shorts zijn het spiegelbeeld (alleen aandelen; spot-crypto kun je niet shorten).

**Filters:** relatief volume ≥ 1,5x, dagelijkse ATR ≥ 2%, niet te ver doorgelopen, RSI niet extreem, minimaal 1:2 tot het eerstvolgende level, en de bot kijkt alleen naar **afgesloten** candles.

**Marktfilter:** geen longs als de markt hard daalt (onder VWAP, EMA9 < EMA20 en vandaag flink lager). Voor crypto kijkt hij naar Bitcoin (≥ 1% gedaald), voor US-aandelen naar SPY en voor Amsterdam naar de AEX (≥ 0,5%). Shorts worden op dezelfde manier geblokkeerd als de markt hard stijgt.

**Cijfers:** aandelen die vandaag of de volgende handelsdag kwartaalcijfers publiceren, worden overgeslagen.

**Watchlist:** een vaste lijst plus elk half uur de actiefste US-aandelen van die dag (grootste stijgers, dalers en meest verhandeld; minimaal $5 en 1 miljoen volume). Crypto: de 30 grootste EUR-paren plus de 10 grootste bewegers met genoeg volume.

## 🟣 Swing-setups

| Setup | Wanneer | Stop | Relatie met je eigen regels |
|---|---|---|---|
| Boven EMA Ribbon (dag) | dagslot sluit voor het eerst boven alle 8 EMA's (20 t/m 55) | dagslot terug onder de ribbon | lijkt op de EMA Ribbon-voorwaarde van je 🟢 daily-alert |
| WaveTrend-omslag in oversold (week) | WaveTrend (VuManChu B, 9/12/3) kruist omhoog onder −53 | weekslot onder de laagste low van 8 weken | lijkt op je 🟢 weekly-alert, maar is **niet identiek** |

De bot kan je eigen TradingView-alerts (gold dot, monthly-alert, red zone) **niet** precies namaken. Gebruik swing-signalen als seintje om je chart te openen, niet als vervanging van je eigen alerts.

## Slagingspercentage (alleen ⚡)

De backtest speelt alle daytrade-setups na op ~60 dagen (aandelen) en 45 dagen (crypto), **inclusief kosten**: 0,05% per kant voor aandelen en 0,25% per kant voor crypto (Bitvavo). Het oordeel kijkt naar wat een setup gemiddeld oplevert per trade na kosten:

- 🟢 **hoog**: ≥ +0,25R
- 🟡 **gemiddeld**: 0 tot +0,25R
- 🔴 **laag**: verliesgevend
- ⚪ **te weinig data**: minder dan 20 trades

Een paar maanden historie is weinig. Je papertrades zijn uiteindelijk betrouwbaarder.

### Filters tegen "de stop wordt steeds geraakt"

Elke week test de backtest per markt (aandelen en crypto) 12 tot 24 combinaties van deze filters op echte koersen:

| Filter | Wat het doet | Waarom |
|---|---|---|
| **Minimale stopafstand** (0,5 / 1 / 1,5× ATR) | Slaat setups over waarvan het invalidatiepunt binnen de normale koersruis van een paar minuten ligt | Zo'n stop wordt vaak "toevallig" geraakt, en de kosten eten een groot deel van je risico op. De stop blijft gewoon op het invalidatiepunt; de bot neemt zo'n trade alleen niet |
| **Kostenfilter** (max. 0,25R) | Slaat trades over waarbij de kosten meer dan een kwart van je risico zijn | Vooral bij crypto met 0,25% per kant |
| **Trendfilter** (aan/uit) | Long alleen als de koers gisteren boven het 20-daags gemiddelde sloot, short omgekeerd | Niet tegen de grote trend in handelen |
| **Break-even** (aan/uit) | Stop naar je instapprijs zodra de koers 1R in je voordeel staat | Minder volle verliezen, al worden ook wat winners afgekapt |
| **Tijdvenster crypto** (aan/uit) | Alleen 14:00–22:00 Nederlandse tijd, als Amerika open is | 's Nachts en in het weekend is crypto vaak grillig met weinig volume |

**Zo kiest de bot zonder zichzelf voor de gek te houden:** hij kiest de beste combinatie op de eerste 70% van de dagen. Daarna controleert hij die keuze op de laatste 30%, data die hij bij het kiezen niet gezien heeft. Alleen als hij ook daar winst geeft na kosten, gaat de markt **aan**. Zo niet, dan staat daytrade voor die markt **uit** (⛔ in Telegram) en krijg je er geen signalen of papertrades van. Swing-signalen gaan altijd door.

Je krijgt standaard alleen daytrade-signalen van 🟢- en 🟡-setups. Alleen die worden gepapertraded.

**Verwachting:** ook met goede instellingen verlies je de meeste trades. Bij een 2R-doel speel je met ~35% winst quitte en is 40–45% al goed. Vijf verliezers op rij is dus normaal. Beoordeel een setup pas na 30 papertrades.

## Papertrading (alleen ⚡)

Bij elk daytrade-signaal opent de bot een papertrade, behalve als er al 3 open staan (je regel) of als er al een open papertrade in hetzelfde symbool loopt. Daarna controleert hij elke scan of eerst de stop of eerst het doel geraakt is. Wordt geen van beide geraakt, dan sluit hij aan het eind van de dag op de slotkoers. Instap is de slotkoers van de signaalcandle; in het echt kan je instap iets afwijken.

Het dagoverzicht laat per setup zien hoeveel papertrades je hebt (doel: 30) en wat ze gemiddeld opleverden.

## Instellingen aanpassen

Op GitHub: **Settings → Secrets and variables → Actions → Variables → New repository variable**, naam `SCAN_ARGS`. Voorbeelden:

- `--telegram-min hoog`: alleen 🟢 daytrade-meldingen (standaard: 🟢 en 🟡)
- `--telegram-min alle`: ook 🔴 en ⚪ meldingen (die worden dan ook gepapertraded)
- `--force-active`: ook daytrade-signalen in een markt die de backtest heeft uitgezet (niet aan te raden)
- `--account 5000`: positiegrootte op basis van €5.000
- `--fee-crypto 0.15`: als je bij Bitvavo minder betaalt (in %)
- `--no-dynamic`: alleen de vaste aandelenlijst
- `--no-stock-shorts`: geen shorts

Meerdere opties kun je achter elkaar zetten, met een spatie ertussen.

## Bestanden downloaden

Na elke backtest (zondag, of handmatig via **Actions → Backtest → Run workflow**) staan je logboeken klaar onder **Actions → Backtest → laatste run → Artifacts**: `signals_log.csv`, `paper_trades.csv`, `swing_log.csv` en `backtest_trades.csv`.

## Bekende beperkingen

- GitHub start geplande runs soms 5 tot 15 minuten te laat. Voor ⚡ daytrades is dat merkbaar; check de prijs altijd in je broker. Voor 🟣 swing maakt het niet uit.
- Yahoo-data voor Euronext loopt vaak ~15 minuten achter.
- Feestdagen kent de bot niet; dan zijn er gewoon geen signalen.
- De bot weet niet welke échte posities jij open hebt. Houd je eigen max-3-regel dus zelf in de gaten.

## Stoppen

**Actions → Scan → ⋯ → Disable workflow**, en hetzelfde voor Backtest.

## Lokaal draaien (optioneel)

```bash
pip install -r requirements.txt
python daytrade_scanner.py --backtest
python daytrade_scanner.py --loop 5
python daytrade_scanner.py --swing-now     # swing-scan + dagoverzicht direct
```
