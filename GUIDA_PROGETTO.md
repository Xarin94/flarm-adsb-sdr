# Guida di progetto - PlutoSDR ADS-B Tracker

## Obiettivo

Realizzare un programma cross-platform che si colleghi a una PlutoSDR, riceva i segnali ADS-B su 1090 MHz, decodifichi i messaggi Mode-S/ADS-B e visualizzi i traffici su una mappa Leaflet in tempo reale.

Stato attuale: il backend resta Python e serve HTTP/SSE locale; la UI puo essere aperta da browser oppure dalla shell desktop Electron. Su Windows, Linux e macOS gli script npm usano lo stesso backend e la stessa pipeline dati.

L'interfaccia deve avere uno stile minimale ispirato a SpaceX / Crew Dragon: sfondo scuro, linee sottili, tipografia tecnica, pochi colori, alta leggibilita e pannelli operativi essenziali.

## Funzioni richieste

- Connessione a PlutoSDR tramite IIO / pyadi-iio.
- Ricezione IQ sulla frequenza ADS-B `1090 MHz`.
- Pipeline di decodifica ADS-B completa per i messaggi utili alla visualizzazione.
- Mappa Leaflet zoomabile.
- Marker per ogni traffico con etichetta contenente:
  - ICAO
  - quota
- Scia storica per ogni contatto.
- Rimozione automatica di un contatto dopo `60 secondi` senza nuovi punti posizione.
- Pannello laterale con:
  - spettro RF
  - waterfall
  - cerchio / indicatore di decodifica
  - stato ricezione e traffici attivi

## Architettura proposta

Il progetto sara composto da un backend Python e da una frontend web locale.

```text
PlutoSDR
   |
   v
Ricezione IQ 1090 MHz
   |
   v
Pre-processing segnale
   |
   v
Rilevamento preambolo ADS-B
   |
   v
Decodifica bit PPM
   |
   v
Validazione CRC Mode-S
   |
   v
Parser messaggi ADS-B
   |
   v
Tracker traffici
   |
   v
HTTP/SSE locale
   |
   v
UI Leaflet + Spectrum + Waterfall
```

## Componenti

### 1. Backend cross-platform

Il punto di ingresso e lo script Python, oppure i wrapper npm che scelgono l'interprete corretto per il sistema:

```bash
npm run backend:sim
npm run backend:pluto
```

Parametri previsti:

```bash
npm start -- --source pluto --uri ip:192.168.2.1
npm start -- --source sim
```

Modalita previste:

- `pluto`: usa solo PlutoSDR e segnala errore se non disponibile.
- `sim`: genera traffico simulato per testare UI e logica senza radio.
- `auto`: prova PlutoSDR e, se non disponibile, passa alla simulazione.

### 2. Sorgente SDR

La sorgente PlutoSDR usera una configurazione iniziale conservativa, pensata per non deformare i burst ADS-B ma senza aprire banda RF inutilmente:

- frequenza centrale: `1090000000 Hz`
- sample rate iniziale: `4000000 S/s`
- bandwidth RF iniziale: `2800000 Hz`
- gain iniziale configurabile da CLI
- controllo gain: `manual` con adattamento software lento, oppure AGC hardware `slow_attack`/`fast_attack` da CLI
- buffer RX continuo

Impostazioni CLI previste:

```bash
npm start -- --source pluto --sample-rate 4000000 --rf-bandwidth 2800000 --gain 35 --gain-mode manual
npm start -- --source pluto --sample-rate 4000000 --rf-bandwidth 3200000 --gain-mode slow_attack
```

#### Nota di fattibilita RF

Per ADS-B non conviene impostare la PlutoSDR alla larghezza di banda minima assoluta. Il segnale Mode-S/ADS-B e composto da pulse stretti: ogni bit occupa `1 us` e la decisione PPM confronta due mezzi bit da `0.5 us`. Se il filtro RF/baseband e troppo stretto, i fronti vengono smussati, il preambolo diventa meno netto e aumenta il rischio di perdere frame o sbagliare bit.

La scelta corretta e usare `4 MS/s` per avere due campioni per ogni mezzo bit ADS-B e tenere la RF bandwidth al minimo utile piu margine:

- `2.0 MS/s` e il limite teorico minimo per campionare mezzi bit da `0.5 us`, ma lascia poco margine.
- `4.0 MS/s` e la scelta di progetto: permette una decisione PPM piu stabile e una migliore verifica del preambolo.
- `2.8 MHz` e il default RF bandwidth proposto: abbastanza stretto da ridurre rumore fuori canale, ma non cosi stretto da smussare eccessivamente pulse da `0.5 us`.
- `3.2 MHz` resta una seconda configurazione di prova se il filtro a `2.8 MHz` riduce il tasso di CRC validi.
- Evitare bandwidth molto strette, per esempio `200 kHz`, perche sono adatte a segnali narrowband ma non a burst PPM ADS-B.

Il progetto deve quindi esporre `--sample-rate` e `--rf-bandwidth`, usando come default `4.0 MS/s` e `2.8 MHz`.

Sul canale `868 MHz` vale il ragionamento opposto: FLARM e ADS-L sono 2-FSK a `100 kchip/s` con deviazione `+/-50 kHz`, quindi il segnale occupa ~`250 kHz`. La banda RF va tenuta al minimo utile (`--flarm-rf-bandwidth`, default `300 kHz`) per far entrare i burst tagliando il rumore della banda SRD-860, mentre il sample rate resta `4 MS/s` (abbassarlo peggiorerebbe l'ADS-B; la banda viene comunque limitata a Nyquist). Il cambio di filtro avviene automaticamente a ogni retune dello scan RX0. Sullo stesso canale il decoder demodula anche i beacon ADS-L (EASA SRD-860, iConspicuity): stesso PHY del FLARM Legacy, syncword `Manchester(0xF5724B18)`, scrambling XXTEA a chiave zero e CRC-24 Mode-S.

Il PlutoSDR disponibile ha TCXO da `0.1 ppm`. A `1090 MHz` questo corrisponde a circa `109 Hz` di errore nominale, quindi il drift di frequenza e trascurabile rispetto a una bandwidth RF nell'ordine di `2.8-3.2 MHz`. Il margine di banda serve soprattutto a preservare i fronti e la forma dei pulse ADS-B, non a compensare il drift dell'oscillatore.

#### Gain adattivo

Il gain deve essere adattato, ma non in modo aggressivo dentro il singolo messaggio ADS-B. Un frame dura circa `120 us`; cambiare gain durante il frame puo alterare la magnitudine relativa dei pulse e peggiorare la decodifica.

Strategia proposta:

- default: `gain-mode manual`
- gain iniziale: `35 dB`
- range operativo default: circa `5-52 dB`, con clamp configurabile
- ogni `0.5-1 s` stimare:
  - percentuale campioni vicini al full scale
  - rumore di fondo / mediana magnitudine
  - numero messaggi CRC validi e non validi
- ridurre gain di `6 dB` se ci sono clipping o troppi burst saturi
- aumentare gain di `0.5 dB` solo se non vengono rilevati preamboli e non c'e clipping
- applicare isteresi per evitare oscillazioni
- sospendere modifiche per alcuni buffer dopo ogni cambio gain

Modalita alternative:

- `slow_attack`: utile come fallback quando il livello RF cambia lentamente.
- `fast_attack`: da testare, ma puo reagire troppo durante burst impulsivi.
- `manual-fixed`: gain fisso per confronti, debugging e misure ripetibili.

Dipendenze:

```bash
sudo apt install libiio-utils
python -m pip install numpy pyadi-iio
```

Su Windows il backend cerca automaticamente `libiio.dll` nelle cartelle note e sotto `C:\Program Files`. Se serve, impostare `PLUTO_ADSB_LIBIIO_DIR` sulla cartella che contiene il DLL.

Comandi utili per verificare la PlutoSDR:

```bash
iio_info -s
iio_attr -u ip:192.168.2.1
```

### 3. Decodifica ADS-B

La pipeline software decodifichera il segnale a 1090 MHz:

1. Calcolo magnitudine dai campioni IQ.
2. Stima del rumore e soglia dinamica.
3. Rilevamento preambolo ADS-B da 8 microsecondi.
4. Decodifica PPM dei bit successivi.
5. Ricostruzione frame Mode-S da 56 o 112 bit.
6. Validazione CRC.
7. Parsing dei messaggi ADS-B Extended Squitter.

Messaggi prioritari:

- DF17 / DF18: ADS-B Extended Squitter.
- Type Code 1-4: callsign.
- Type Code 9-18: posizione airborne e quota.
- Type Code 19: velocita, se utile per estensioni successive.

La posizione sara calcolata usando CPR globale, mantenendo per ogni ICAO l'ultimo frame even e odd.

### 4. Tracker traffici

Ogni traffico sara identificato da ICAO a 24 bit.

Stato minimo per contatto:

```json
{
  "icao": "ABC123",
  "callsign": "AZA123",
  "lat": 45.123,
  "lon": 9.456,
  "altitude_ft": 34000,
  "last_seen": 1710000000.0,
  "last_position": 1710000000.0,
  "trail": [
    [45.123, 9.456, 1710000000.0]
  ]
}
```

Regole:

- Aggiungere un punto alla scia solo quando arriva una nuova posizione valida.
- Limitare la scia a un numero massimo di punti per evitare crescita indefinita.
- Rimuovere il contatto dopo `60 secondi` senza nuovi punti posizione.
- Inviare snapshot periodici alla UI, anche se non arrivano nuovi messaggi.

### 5. Backend locale

Il backend Python offrira:

- file statici della UI
- endpoint eventi realtime con Server-Sent Events
- endpoint stato JSON opzionale

Endpoint previsti:

```text
GET /                 UI principale
GET /events           stream realtime SSE
GET /api/status       stato backend
GET /api/tracks       snapshot traffici
```

SSE evita dipendenze extra rispetto a WebSocket e funziona bene per una telemetria monodirezionale backend -> browser.

### 6. Frontend Leaflet

La UI sara una web app locale con:

- mappa Leaflet zoomabile.
- tile OpenStreetMap.
- marker tecnico per ogni traffico.
- label sempre visibile con ICAO e quota.
- polyline per la scia.
- pannello laterale con spettro, waterfall e cerchio decoding.

Layout previsto:

```text
+---------------------------------------------------------------+
| PLUTO ADS-B / 1090 MHz                         RX STATUS      |
+------------------------------------------+--------------------+
|                                          | Spectrum           |
|                                          | [canvas]           |
|                  MAP                     +--------------------+
|                                          | Waterfall          |
|                                          | [canvas]           |
|                                          +--------------------+
|                                          | Decode Circle      |
|                                          | Active Tracks      |
+------------------------------------------+--------------------+
```

## Stile grafico

Linee guida:

- sfondo quasi nero
- pannelli grigio antracite
- linee sottili bianche/grigie
- accento singolo ciano o bianco freddo per stato RX
- marker mappa sobri, non decorativi
- testo tecnico compatto
- niente hero page, niente elementi marketing
- niente decorazioni superflue

Palette indicativa:

```css
--bg: #050607;
--panel: #0d1013;
--line: #2a3036;
--text: #f2f5f8;
--muted: #8b949e;
--accent: #71d9ff;
--warn: #ffb86b;
--danger: #ff5f56;
```

## Formato eventi realtime

Esempio payload SSE:

```json
{
  "type": "snapshot",
  "time": 1710000000.0,
  "stats": {
    "source": "pluto",
    "messages_total": 1204,
    "messages_per_sec": 18.2,
    "crc_ok": 921,
    "crc_bad": 283,
    "active_tracks": 12
  },
  "tracks": [
    {
      "icao": "4CA123",
      "callsign": "RYR42AB",
      "lat": 45.47,
      "lon": 9.19,
      "altitude_ft": 36000,
      "last_position_age": 2.1,
      "trail": [[45.46, 9.18], [45.47, 9.19]]
    }
  ],
  "spectrum": [-82.1, -80.4, -78.2],
  "waterfall": [-85.0, -81.2, -79.8]
}
```

## File previsti

```text
pluto_adsb_tracker.py      Backend, SDR, decoder, tracker, server HTTP
requirements.txt           Dipendenze Python
package.json                Script npm ed Electron
electron/
  main.js                   Shell desktop, avvio backend, finestra app
  start.js                  Launcher Electron cross-platform
  run-python.js             Launcher backend Python cross-platform
  python-command.js         Selezione interprete Python
static/
  index.html               Shell UI
  app.js                   Leaflet, SSE, rendering canvas
  style.css                Stile SpaceX / Dragon minimal
README.md                  Avvio rapido e note hardware
GUIDA_PROGETTO.md          Questa guida
```

## Verifica

Verifiche minime prima di considerare il progetto pronto:

1. Avvio in modalita simulata:

```bash
npm run backend:sim
```

2. Apertura UI:

```text
http://127.0.0.1:8080
```

3. Controlli UI:

- la mappa si apre ed e zoomabile
- i marker mostrano ICAO e quota
- le scie si aggiornano
- i contatti spariscono dopo 60 secondi senza punti
- spettro e waterfall si muovono
- il cerchio di decodifica reagisce ai messaggi

4. Avvio con PlutoSDR:

```bash
npm run start:pluto
```

5. Controlli SDR:

- PlutoSDR rilevata
- frequenza impostata a 1090 MHz
- buffer RX attivo
- messaggi CRC validi ricevuti
- traffici reali visualizzati sulla mappa

6. Sweep iniziale RF/gain:

```bash
npm start -- --source pluto --sample-rate 4000000 --rf-bandwidth 2800000 --gain-mode manual --gain 25
npm start -- --source pluto --sample-rate 4000000 --rf-bandwidth 2800000 --gain-mode manual --gain 35
npm start -- --source pluto --sample-rate 4000000 --rf-bandwidth 3200000 --gain-mode manual --gain 40
npm start -- --source pluto --sample-rate 4000000 --rf-bandwidth 3200000 --gain-mode slow_attack
```

Metriche da confrontare per almeno `5 min` per configurazione:

- CRC validi/minuto
- rapporto CRC validi / preamboli candidati
- percentuale campioni saturi o quasi saturi
- rumore di fondo stimato
- traffici con posizione valida
- stabilita del rate dopo eventuali cambi gain

La configurazione migliore e quella con piu CRC validi e meno saturazione, non necessariamente quella con piu preamboli rilevati.

## Limiti iniziali previsti

- La qualita della decodifica dipendera da antenna, gain, rumore locale e clock della PlutoSDR.
- Leaflet richiede accesso ai tile mappa online, salvo cache o tile server locale.
- La prima posizione di un traffico richiede una coppia CPR even/odd valida.
- Alcuni messaggi con quota Gillham non-Q potranno essere ignorati nella prima versione.

## Riferimenti tecnici

- Analog Devices AD9363: https://www.analog.com/en/products/ad9363.html
- Analog Devices pyadi-iio AD936x/Pluto API: https://analogdevicesinc.github.io/pyadi-iio/devices/adi.ad936x.html
- Analog Devices AD9361/AD936x Linux IIO driver: https://wiki.analog.com/resources/tools-software/linux-drivers/iio-transceiver/ad9361

## Estensioni future

- Supporto WebSocket oltre SSE.
- Registrazione CSV o JSONL dei traffici.
- Replay da file IQ.
- Filtro distanza da posizione ricevitore.
- Stima RSSI per traffico.
- Supporto MLAT esterno.
- Configurazione grafica di gain, sample rate e soglia.
- Tile server locale per uso offline.
