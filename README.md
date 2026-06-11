# Pluto ADS-B Tracker

Programma cross-platform per ricevere ADS-B 1090 MHz con PlutoSDR, decodificare messaggi Mode-S/ADS-B e visualizzare i traffici su mappa Leaflet con scie, quota e ICAO. Il backend e scritto in Python, la UI puo girare nel browser o nella shell desktop Electron.

## Avvio rapido

Modalita desktop Electron:

```bash
npm install
npm run start:sim
```

La finestra Electron avvia automaticamente il backend Python locale, sceglie la porta HTTP disponibile e carica la UI esistente. Per usare la sorgente reale:

```bash
npm run start:pluto
```

Se vuoi un display grafico piu rapido per spettro e waterfall:

```bash
npm run start:pluto:fast
```

Puoi passare gli stessi argomenti del backend dopo `--`, per esempio:

```bash
npm start -- --source pluto --uri ip:192.168.2.1 --dual-rx
```

Il refresh grafico HTTP/SSE e configurabile con `--ui-rate-hz` tra `1` e `30`, default `12`:

```bash
npm start -- --source pluto --ui-rate-hz 20
```

Backend diretto, cross-platform tramite npm:

```bash
npm run backend:sim
npm run backend:pluto
```

Backend diretto con Python su Windows PowerShell:

```powershell
py -3 .\pluto_adsb_tracker.py --source sim --port 8080
py -3 .\pluto_adsb_tracker.py --source pluto --uri ip:192.168.2.1 --port 8080
```

Backend diretto con Python su Linux/macOS:

```bash
python3 ./pluto_adsb_tracker.py --source sim --port 8080
python3 ./pluto_adsb_tracker.py --source pluto --uri ip:192.168.2.1 --port 8080
```

Apri:

```text
http://127.0.0.1:8080
```

Da questa schermata puoi premere `CONNETTI PLUTO` per passare alla PlutoSDR reale senza riavviare il server. Se la connessione fallisce, la UI mostra l'errore e mantiene la sorgente corrente.

Modalita automatica, con fallback simulato se la Pluto non e disponibile:

```bash
npm start -- --source auto
```

## Dipendenze

Python 3.10+, Node.js e npm sono richiesti per la modalita desktop. Su Windows il launcher usa prima `.venv\Scripts\python.exe`, poi `py -3`. Su Linux/macOS usa prima `.venv/bin/python`, poi `python3`. Puoi forzare l'interprete impostando `PLUTO_ADSB_PYTHON`.

Dipendenze Python:

```bash
python -m pip install -r requirements.txt
```

Su Linux, pacchetti di sistema consigliati:

```bash
sudo apt install libiio-utils python3-pip
```

Dipendenze desktop:

```bash
npm install
```

Verifica PlutoSDR:

```bash
iio_info -s
iio_attr -u ip:192.168.2.1
```

Su Windows serve anche che libiio/pyadi-iio riesca a vedere la PlutoSDR. Il programma prova a trovare automaticamente `libiio.dll` anche sotto `C:\Program Files`; se il DLL e in una cartella non standard puoi indicarla cosi:

```powershell
$env:PLUTO_ADSB_LIBIIO_DIR = "C:\percorso\cartella-libiio"
npm run start:pluto
```

Se `iio_info -s` non mostra il dispositivo, il backend puo comunque partire in simulazione ma la connessione Pluto fallira.

Per la connessione predefinita `ip:192.168.2.1`, `ipconfig` deve mostrare una scheda USB Ethernet/RNDIS con indirizzo `192.168.2.x` e il comando seguente deve rispondere:

```powershell
ping 192.168.2.1
```

Se Windows vede solo il disco/seriale o non mostra nessun dispositivo ADALM-Pluto, usare la porta USB dati del Pluto, provare un cavo USB dati diverso e controllare il driver RNDIS/WinUSB in Gestione dispositivi. Il backend prova anche il fallback `usb:`, ma anche quello richiede che libusb/WinUSB veda il dispositivo.

## Configurazione radio

Default di progetto:

```text
center frequency: 1090 MHz
sample rate:      4.0 MS/s
RF bandwidth:     2.8 MHz
gain mode:        manual con adattamento software lento
gain iniziale:    35 dB
gain range auto:  5-52 dB
```

Esempi:

```bash
npm start -- --source pluto --sample-rate 4000000 --rf-bandwidth 2800000 --gain 35 --gain-mode manual
npm start -- --source pluto --sample-rate 4000000 --rf-bandwidth 3200000 --gain 38 --gain-mode manual
npm start -- --source pluto --sample-rate 4000000 --rf-bandwidth 2800000 --gain-mode manual-fixed --gain 30
npm start -- --source pluto --sample-rate 4000000 --rf-bandwidth 3200000 --gain-mode slow_attack
```

Il sample rate resta a `4 MS/s` per avere due campioni su ogni mezzo bit ADS-B da `0.5 us`. La bandwidth RF default `2.8 MHz` tiene basso il rumore fuori banda senza restringere troppo i fronti dei pulse.

Con TCXO `0.1 ppm`, l'errore nominale a `1090 MHz` e circa `109 Hz`, quindi il margine RF serve principalmente a preservare la forma del segnale ADS-B, non a coprire drift dell'oscillatore.

## Pipeline

1. Ricezione IQ da PlutoSDR.
2. Magnitudine dei campioni.
3. Soglia dinamica su rumore/mediana.
4. Rilevamento preambolo ADS-B da `8 us`.
5. Decodifica PPM dei `112 bit`.
6. CRC Mode-S.
7. Parsing DF17/DF18.
8. Decodifica callsign, quota e CPR globale.
9. Tracker con scia e rimozione contatti dopo `60 s` senza nuovi punti.
10. Pubblicazione UI tramite Server-Sent Events.

Questa pipeline e unica sia in browser sia in Electron: Electron avvia lo stesso `pluto_adsb_tracker.py`, aspetta `/api/status` e carica la UI via HTTP locale. Gli endpoint `/events`, `/api/status`, `/api/tracks`, `/api/connect/pluto`, `/api/connect/sim` e `/api/gain` sono gli stessi.

## UI

La pagina web include:

- mappa Leaflet zoomabile
- marker per ogni traffico
- label laterale con ICAO e quota
- scia del contatto
- contatti ADS-B in ciano
- contatti FLARM in verde
- spettro e waterfall ADS-B
- spettro e waterfall FLARM/RX1
- slider verticale per impostare il gain RX
- cerchio di decodifica

In modalita Electron la stessa UI viene caricata dentro una finestra desktop. Il processo Electron avvia e arresta il server Python insieme all'applicazione.

Leaflet e i tile OpenStreetMap vengono caricati da CDN/Internet. Il backend e la simulazione funzionano anche offline, ma la mappa potrebbe non mostrare i tile senza rete o cache locale.

## Dual RX e FLARM

Con `--dual-rx` attivo, il backend prova a impostare `rx_enabled_channels = [0, 1]`. RX0 resta dedicato alla pipeline ADS-B, RX1 alimenta i pannelli FLARM e il rilevatore sperimentale di burst FLARM.

Nota radio importante: sui dispositivi AD936x i canali RX condividono LO e sample rate. ADS-B a `1090 MHz` e FLARM europeo a `868 MHz` non sono ricevibili simultaneamente come due frequenze indipendenti con una sola LO Pluto. Per usare RX1 con FLARM serve una configurazione hardware coerente, per esempio conversione esterna verso la stessa finestra RF, oppure una sessione dedicata alla banda FLARM.

Lo slider gain della UI chiama:

```text
POST /api/gain
```

e imposta lo stesso gain sui canali RX esposti dal driver.

## Test

Self-test del decoder:

```bash
npm run self-test
```

Avvio locale in simulazione:

```bash
npm run backend:sim
```

Endpoint utili:

```text
GET /api/status
GET /api/tracks
GET /events
POST /api/connect/pluto
POST /api/connect/sim
POST /api/gain
```

## Note operative

- Il gain automatico software parte conservativo, scende rapidamente in caso di clipping e sale solo lentamente quando non rileva preamboli.
- Un contatto viene rimosso dopo `60 s` senza nuovi punti posizione.
- Se arrivano messaggi senza coppia CPR even/odd valida, il traffico non viene ancora disegnato sulla mappa.
- In `--source auto`, se `pyadi-iio` o la Pluto non sono disponibili, parte la simulazione.
