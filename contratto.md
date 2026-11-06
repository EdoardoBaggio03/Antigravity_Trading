# Contratto Costituzionale di "AI Capital — 1% Daily Target Edition"

Questo documento definisce i vincoli operativi, quantitativi e deterministici dell'agente di trading autonomo **AI Capital** in configurazione **High-Alpha Multi-Asset**.

## 1. Obiettivo Strategico: +1.00% Netto al Giorno
- **Target Primario:** Realizzare un rendimento netto del **+1,00% al giorno sull'Equity totale** (es. `+$1.000/giorno` su `$100.000` di capitale), sfruttando i titoli a maggiore volatilità, forza relativa (Momentum Breakout), ritracciamenti rapidi e rotazione tra asset class.
- **Guadagno in Ogni Direzione del Mercato:** Nelle giornate ribassiste, l'agente può allocare liquidità su **ETF Inversi (`SQQQ`)** o difensivi (`GLD`, `TLT`), potendo così centrare il `+1,00%` giornaliero anche durante i sell-off di Wall Street senza mai ricorrere a vendite allo scoperto (Short a margine).
- **Profit Locking (Difesa del Target):** Quando il P&L giornaliero netto raggiunge o supera il **`+1,00%`**, l'agente passa in modalità **Profit Lock**, liquidando le posizioni più volatili per cristallizzare il guadagno sul conto.

## 2. Universo Multi-Asset Autorizzato (30 Strumenti ad Alta Liquidità)
L'agente scansiona e opera su **30 strumenti USA** suddivisi in 5 cluster tattici:
1. **ETF Indici & Tattici 3x (Long & Inverse):** `SPY`, `QQQ`, `IWM`, `TQQQ` (Nasdaq 3x Long), `SOXL` (Semiconduttori 3x Long), `SPXL` (S&P 500 3x Long), `SQQQ` (Nasdaq 3x Inverse per fasi ribassiste).
2. **Mega-Cap & AI Momentum:** `NVDA`, `AAPL`, `MSFT`, `META`, `AMZN`, `GOOGL`, `TSLA`, `AMD`, `AVGO`, `PLTR`, `NFLX`, `CRM`.
3. **High-Beta Fintech & Crypto-Proxy:** `COIN`, `MSTR`, `HOOD`, `SOFI`, `SHOP`.
4. **Commodities & Macro:** `GLD` (Oro), `SLV` (Argento), `USO` (Petrolio), `TLT` (Treasury 20+ Yr).
5. **Settori Ciclici:** `XLE` (Energia), `XLF` (Finanziari).

## 3. La Gabbia (Validazione Deterministica Pre-Ordine)
Ogni ordine generato dall'AI o dal motore di Profit-Lock deve superare il filtro deterministico (`risk_cage.py`):
1. **Protezione Ambiente (Paper Only):** Endpoint bloccato su `https://paper-api.alpaca.markets`. Arresto immediato (`RuntimeError`) se viene rilevato qualsiasi altro endpoint.
2. **Limite di Concentrazione per Singolo Asset:** Max **25% dell'Equity totale** su un singolo strumento (configurabile, con soglia standard 20%-25% verificata al centesimo incluso il già detenuto).
3. **Solo Acquisti Coperti da Liquidità Reale (`Cash >= costo_ordine`):** Zero leva del broker, nessun margine passivo e divieto assoluto di vendere allo scoperto azioni non possedute (`SELL` consentito solo fino alla quantità effettivamente in portafoglio).
4. **Auto-Critica Obbligatoria (`perche_potrei_avere_torto`):** Ogni ordine deve esplicitare chiaramente lo scenario avverso e il livello di invalidazione tecnica.
5. **Regole Automatiche di Take-Profit / Stop-Loss Intraday:**
   - **Portfolio Target Lock (`+1.00%` Equity giornaliero):** se l'Equity totale guadagna `>= +1.00%` rispetto alla chiusura precedente (`last_equity`), il sistema può incassare i profitti sulle posizioni aperte.
   - **Single-Asset Take Profit (`+2.20%`) & Stop Loss (`-1.50%`):** protezione automatica sulle singole posizioni per tagliare rapidamente le perdite e far correre/incassare i trade vincenti.

## 4. Routine Giornaliera Potenziata (`Europe/Rome`)
- **`09:00` — Pre-Market & Piano Multi-Asset:** Scansione dei 30 strumenti, selezione dei migliori setup ad alto potenziale e invio del Piano su Telegram.
- **`15:35` — Apertura USA & Esecuzione Aggressiva:** Ingresso sui setup validati da **La Gabbia** dopo i primi 5 minuti di apertura.
- **`17:00` & `18:30` — Check Intraday, Stop-Loss & Rotazione:** Verifica intermedia; incassa i trade già a target (`+1%` portafoglio o `+2.2%` singolo titolo), taglia i rami secchi (`-1.5%`) e ruota se necessario.
- **`20:30` — Power Hour & Profit Locking:** Valutazione pre-chiusura USA per consolidare l'obiettivo del `+1.00%` giornaliero.
- **`21:50` — Bilancio Post-Market (21:05/21:50) & Memoria:** Calcolo P&L netto giornaliero rispetto al target del `+1.00%` e al benchmark `SPY`, aggiornamento memoria compatta e pubblicazione automatica sul Gestionale Web.
