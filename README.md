# Daytrade-scanner (aandelen + crypto)

Scant de markt op intraday-kansen en rekent per kans stop, doel en positiegrootte uit volgens jouw risicoregels (max 1% risico, minimaal 1:2 R:R, max 3 posities). **De bot plaatst geen orders** en geeft geen advies. Hij laat zien wat aan de criteria voldoet en de beslissing ligt bij jou.

## Installeren

```bash
pip install -r requirements.txt
```

Je hebt Python 3.10 of nieuwer nodig. API-keys zijn niet nodig: aandelen komen van Yahoo Finance, crypto van de publieke Bitvavo-API.

## Gebruiken

```bash
python daytrade_scanner.py --backtest                   # eerst: slagingspercentages berekenen
python daytrade_scanner.py                              # aandelen + crypto, één scan
python daytrade_scanner.py --market crypto --loop 5     # crypto elke 5 minuten
python daytrade_scanner.py --account 5000 --open-positions 2
python daytrade_scanner.py --stocks-file mijn_watchlist.txt
python daytrade_scanner.py --crypto-symbols "BTC/EUR,ETH/EUR,SOL/EUR"
python daytrade_scanner.py --show-all                   # ook setups met R:R < 2 tonen
python daytrade_scanner.py --market stocks --include-closed   # testen als de beurs dicht is
```

Alle opties zie je met `python daytrade_scanner.py --help`.

## Wat hij zoekt

**Filters (moeten allebei kloppen):**
- **RVOL ≥ 1,5x**: het volume tot nu toe vergeleken met hetzelfde tijdstip op de vorige dagen. Er moet interesse in het aandeel of de munt zijn.
- **Dagelijkse ATR ≥ 2% van de prijs**: er moet genoeg beweging zijn om intraday iets te verdienen.

**Setups (op 5-minutencandles):**

| Setup | Long als… | Stop (invalidatie) |
|---|---|---|
| ORB | prijs breekt boven de eerste 15 minuten, boven VWAP, EMA9 > EMA20 | midden van de opening range |
| Breakout high gisteren | prijs gaat door de high van gisteren | 0,5× ATR(5m) onder die high |
| VWAP-pullback | trend omhoog, candle tikt VWAP/EMA20 aan en sluit erboven | onder candle-low/VWAP |

Shorts werken gespiegeld (ORB short, breakdown door de low van gisteren, VWAP-afwijzing). Voor aandelen staan ze standaard aan en voor crypto uit, omdat je spot-crypto niet kunt shorten.

De bot slaat kansen over als ze al te ver doorgelopen zijn (meer dan 1,5× ATR van het triggerniveau), als de 5m-RSI boven de 75 staat (long) of onder de 25 (short), of als de stop verder dan 4% weg ligt.

**R:R-check:** het eerstvolgende niveau in de looprichting (high van gisteren, high van de week, high van vandaag) moet minstens 2R verwijderd zijn. Staat er geen niveau in de weg, dan geldt het 2R-doel.

**Positiegrootte:** `aantal = (account × 1%) / (entry − stop)`, zonder hefboom. Aandelen worden afgerond op hele stuks, crypto mag fractioneel.

## Slagingspercentage per setup

`python daytrade_scanner.py --backtest` speelt alle setups na op de afgelopen ~60 dagen (aandelen, het maximum dat Yahoo levert voor 5-minutendata) en 45 dagen (crypto, in te stellen met `--bt-days`). Dat duurt een paar minuten. De resultaten komen in `setup_stats.json` en elke scan laat ze daarna bij ieder signaal zien:

| Kolom | Betekenis |
|---|---|
| `slagingskans` | % trades dat het 2R-doel haalde vóór de stop, met het aantal trades (n) |
| `verw_R` | gemiddelde uitkomst per trade in R (1R = je risico, dus 1% van je account) |
| `oordeel` | **laag** (verw. R ≤ 0, verliesgevend), **gemiddeld** (0 tot +0,25R), **hoog** (≥ +0,25R) of **te weinig data** (minder dan 20 trades) |

Het oordeel is gebaseerd op de verwachte R en niet op de winrate alleen. Met een 2R-doel speel je bij ~33% winst al quitte, dus 40% kan prima zijn en 30% niet.

Spelregels van de backtest:
- Instap op de slotkoers van de signaalcandle.
- Winst als het 2R-doel eerder geraakt wordt dan de stop. Raakt één candle allebei, dan telt het als verlies (voorzichtig gerekend).
- Niets geraakt aan het eind van de dag? Dan uitstap op de slotkoers ("tijd").
- Per symbool, dag en setup telt alleen het eerste signaal.
- Kosten, spread en slippage zitten er **niet** in, dus in de praktijk valt het iets lager uit.
- Alle losse trades staan in `backtest_trades.csv`.

Draai de backtest elke 1–2 weken opnieuw. De scanner waarschuwt als de cijfers ouder zijn dan 14 dagen. Een paar maanden historie is weinig: zie het als een indicatie, niet als een belofte. Je eigen papertrades in het logboek zijn uiteindelijk betrouwbaarder.

## Logboek voor papertraden

Elk nieuw signaal komt in `signals_log.csv`, met lege kolommen `uitkomst` en `notities`. Hetzelfde signaal wordt per dag één keer gelogd. Vul de uitkomst zelf in, zodat je per setup kunt zien of hij na 30+ papertrades werkt voordat je er echt geld op zet.

## Telegram-meldingen (optioneel)

1. Zoek in Telegram **@BotFather**, stuur `/newbot` en volg de stappen. Je krijgt een token zoals `123456:ABC-def...`.
2. Open je nieuwe bot en stuur hem een willekeurig bericht, bijvoorbeeld "hoi".
3. Draai `python daytrade_scanner.py --telegram-setup 123456:ABC-def...`. De scanner zoekt je chat-id zelf op, slaat alles op in `telegram_config.json` en stuurt een testbericht.

Daarna krijg je bij elke scan een bericht per **nieuw** signaal, met prijs, stop, 2R-doel, aantal, risico en slagingskans (🟢 hoog, 🟡 gemiddeld, 🔴 laag, ⚪ te weinig data). Hetzelfde signaal komt per dag maar één keer binnen.

```bash
python daytrade_scanner.py --loop 5 --telegram-min gemiddeld   # elke 5 min, alleen 🟡 en 🟢
python daytrade_scanner.py --loop 5 --telegram-min hoog        # alleen 🟢
```

Je kunt het token en de chat-id ook als omgevingsvariabelen meegeven (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`). Deel `telegram_config.json` niet met anderen: met het token kan iedereen berichten sturen via je bot.

De scanner draait alleen zolang je computer aan staat en het venster open is.

## Gratis op een server draaien (GitHub Actions)

GitHub draait de scanner elke 5 minuten voor je, ook als je computer uit staat. Elke zondag draait er automatisch een nieuwe backtest. Je hebt geen creditcard nodig.

### Stap 1: Telegram-token en chat-id
- Token: stuur `/newbot` naar **@BotFather** (al gedaan? dan heb je hem al).
- Chat-id: stuur je bot een bericht, open dan in je browser `https://api.telegram.org/bot<JOUW-TOKEN>/getUpdates` en zoek `"chat":{"id":123456789`. Dat getal is je chat-id. (Heb je lokaal `--telegram-setup` gedraaid? Dan staan beide ook in `telegram_config.json`.)

### Stap 2: Repository aanmaken
1. Maak een gratis account op github.com en klik rechtsboven op **+ → New repository**.
2. Naam bijvoorbeeld `daytrade-scanner`, kies **Public** (zie "Publiek of privé" hieronder) en klik **Create repository**.

### Stap 3: Bestanden uploaden
1. Klik op **uploading an existing file**.
2. Pak de zip uit en sleep **alle inhoud** van de map in het venster, inclusief de map `.github`.
   - Mac: mappen die met een punt beginnen zijn verborgen in Finder. Druk **Cmd + Shift + .** om ze te tonen.
3. Klik **Commit changes**. Controleer daarna dat je in de repo de map `.github/workflows` ziet met `scan.yml` en `backtest.yml`.

### Stap 4: Telegram-gegevens als geheim toevoegen
**Settings → Secrets and variables → Actions → New repository secret**, twee keer:
- `TELEGRAM_BOT_TOKEN` = je token
- `TELEGRAM_CHAT_ID` = je chat-id

Secrets zijn versleuteld en ook in een publieke repo voor niemand zichtbaar.

### Stap 5: Starten
1. Ga naar het tabblad **Actions**. Vraagt GitHub om workflows aan te zetten, klik dan op de groene knop.
2. Klik links op **Backtest → Run workflow**. Na 5–10 minuten krijg je in Telegram een overzicht van de slagingspercentages.
3. Klik op **Scan → Run workflow** om één scan te testen. Daarna draait hij vanzelf elke 5 minuten.

### Instellingen aanpassen (optioneel)
Via **Settings → Secrets and variables → Actions → tabblad Variables → New repository variable**:
- `SCAN_ARGS`, bijvoorbeeld `--telegram-min gemiddeld --account 5000 --open-positions 1`
- `BACKTEST_ARGS`, bijvoorbeeld `--bt-days 60`

Zonder `SCAN_ARGS` krijg je alle signalen in Telegram. Ook signalen met "te weinig data" komen dan door, zodat je in het begin niets mist.

### Publiek of privé?
- **Publiek** (aanbevolen): onbeperkt gratis, dus elke 5 minuten scannen kan. Je code en de uitvoer van elke run (de signaaltabel) zijn wel voor iedereen zichtbaar. Je Telegram-gegevens niet.
- **Privé**: GitHub geeft 2.000 gratis minuten per maand en een run kost ~1–2 minuten, dus dan kun je hooguit ongeveer elk uur scannen. Zet daarvoor in `scan.yml` de regel `cron: "*/5 * * * *"` op `cron: "7 * * * *"`.

### Logboek bekijken
Het papertrade-logboek wordt tussen runs bewaard. Na elke backtest kun je het downloaden: **Actions → Backtest → laatste run → Artifacts → logboek-en-backtest**. Wil je het tussendoor hebben, start dan Backtest handmatig.

### Stoppen
**Actions → Scan → ⋯ (rechtsboven) → Disable workflow.** Doe hetzelfde voor Backtest.

### Bekende beperkingen
- GitHub start geplande runs soms 5–15 minuten te laat, vooral rond het hele uur. Op drukke momenten slaat het er ook weleens een over. Voor daytrading is dat merkbaar: zie de melding als seintje om zelf te kijken, niet als exact instapmoment.
- Yahoo blokkeert soms tijdelijk verzoeken vanaf servers van GitHub. Dat zie je in de log van een Scan-run als regels zoals `! AAPL: Too Many Requests`. Crypto (Bitvavo) heeft daar geen last van.
- Gaat een run mis, dan krijg je een ❌-bericht in Telegram met een link naar de foutmelding.

## Goed om te weten

- Yahoo-data is gratis maar niet altijd realtime (Euronext loopt vaak ~15 min achter). Controleer de prijs altijd in je eigen broker of op je chart voordat je iets doet.
- Bij crypto begint de "sessie" (VWAP, opening range) om 00:00 UTC, dus 01:00 Nederlandse tijd. In het eerste kwartier na middernacht zijn er nog geen ORB-signalen.
- RVOL loopt vlak na de opening vaak hoog op. Bekijk de eerste 15 minuten dus met wat scepsis.
- Een `Config`-blok bovenin het script bevat alle drempels als je die wilt aanpassen.
