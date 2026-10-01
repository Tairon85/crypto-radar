# Crypto Radar – Continuous Analyst V0.1

Prima versione del test da **€50 virtuali**. L'app osserva continuamente le crypto, costruisce una serie di prezzi, calcola trend/momentum/RSI/volatilità, genera `ENTRA / ASPETTA / TIENI`, apre operazioni **paper** fino a €10 e applica un trailing dinamico.

## Sicurezza della V0.1
- `APP_MODE=paper`: non invia ordini reali.
- Capitale virtuale iniziale: €50.
- Max €10 per trade, max 3 posizioni.
- Stop rischio base ~3%; trailing profitto progressivo.
- Stop nuovi ingressi se la perdita realizzata del giorno supera €2.
- Nessun obbligo di fare trade: se non trova setup, resta liquida.

## Feed dati
Senza chiavi eToro parte con un simulatore interno, così dashboard e motore sono testabili subito.

Per collegare eToro, impostare su Railway:
- `ETORO_API_KEY`
- `ETORO_USER_KEY`
- `ETORO_WATCHLIST_JSON`, mapping degli **instrumentId reali eToro** ai simboli, ad es. `{ "1234": "TAO", "5678": "UNI" }`

L'app usa l'endpoint ufficiale snapshot rates di eToro e mantiene un rolling window interno. Una V0.2 potrà sostituire il polling con WebSocket/candele e aggiungere news/sentiment.

## Avvio locale
```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```
Aprire http://localhost:8000

## Railway
Il progetto Railway `Crypto Radar Continuous Analyst` è già stato creato. Per il deploy serve un repository GitHub collegato a Railway. Il codice di questo ZIP è pronto per essere caricato in un repo e distribuito.

## Importante
Il punteggio del Radar misura la qualità del setup secondo le regole interne; **non è una probabilità di guadagno**. I risultati paper non includono necessariamente slippage/liquidità reali.
