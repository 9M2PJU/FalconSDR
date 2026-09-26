# SmartScanATC — dokumentacja funkcjonalna

Ten dokument opisuje **co program robi i jak dokładnie się zachowuje** — z takim
poziomem szczegółu (formuły, stałe, progi, kolejność operacji, przypadki
brzegowe), żeby ktoś piszący odpowiednik tego programu od zera, w dowolnym
języku/technologii, uzyskał funkcjonalnie niemal identyczny produkt. Nie opisuje
*jak* jest to zaimplementowane w Pythonie (nazw klas, modułów, bibliotek) poza
przypadkami, gdzie konkretna technologia wymusza konkretne zachowanie
(np. format WAV, RIFF).

Program to skaner pasm radiowych (domyślnie: lotnictwo cywilne ATC 118–136 MHz)
podłączony do odbiornika SDR, który nasłuchuje wielu częstotliwości naraz,
zatrzymuje się na wykrytej transmisji, odtwarza ją na żywo i nagrywa —
**wyłącznie** czas rzeczywistej transmisji, nigdy szumu przestrajania — a
następnie pozwala przeglądać, filtrować, oceniać jakość i transkrybować
zgromadzone nagrania.

---

## 1. Przegląd architektury

Program składa się z trzech w większości niezależnych podsystemów, działających
równolegle (na osobnych wątkach), połączonych przez wspólną konfigurację i
zdarzenia:

1. **Silnik skanera** — maszyna stanów odczytująca próbki IQ z odbiornika SDR,
   wykrywająca transmisje, demodulująca je do dźwięku, odtwarzająca na żywo i
   nagrywająca na dysk.
2. **Biblioteka odtwarzacza** — indeksowanie zapisanych nagrań, ocena ich
   jakości, transkrypcja mowy, statystyki, przeglądanie/filtrowanie/odtwarzanie.
3. **Interfejs użytkownika** — pięć zakładek (Nasłuch, Kanały, Odtwarzacz,
   Statystyki, Opcje) prezentujących i sterujących powyższymi dwoma
   podsystemami, plus trwała konfiguracja zapisywana na dysku.

Silnik skanera działa **niezależnie od tego, która zakładka GUI jest aktualnie
widoczna** — przełączanie zakładek nie zatrzymuje ani nie spowalnia nasłuchu.

### 1.1 Wątki i ich odpowiedzialność (wymóg architektoniczny, nie szczegół implementacyjny)

To, że poniższe operacje muszą być od siebie **odseparowane w czasie** (żadna z
nich nie może blokować żadnej innej), jest wymogiem funkcjonalnym — złamanie
tego dawało realne, słyszalne artefakty dźwiękowe w trakcie rozwoju tego
programu:

- **Akwizycja sprzętu SDR** (odczyt próbek IQ z urządzenia) musi działać w
  pętli bez żadnej przerwy dłuższej niż czas trwania jednej "działki" próbek
  (patrz §3.4) — sterownik SDR ma tylko mały wewnętrzny bufor pierścieniowy
  (rzędu ~100 ms) i milcząco gubi/nadpisuje próbki, jeśli aplikacja nie odbiera
  ich wystarczająco szybko. Zgubione/sklejone próbki objawiają się jako
  słyszalny trzask (nieciągłość fazy sygnału wygląda dla FFT jak szerokopasmowy
  impuls).
- **Demodulacja + redukcja szumów + zapis do pliku** dla każdej aktywnej
  transmisji (może ich być wiele naraz w jednym oknie przechwytu — patrz §7) —
  musi kończyć się w czasie realnym, ale nigdy nie może opóźnić kolejnego
  odczytu z akwizycji.
- **Odtwarzanie na żywo** (wysyłanie audio do karty dźwiękowej) musi działać
  niezależnie od powyższego — chwilowe spowolnienie demodulacji nie może
  spowodować, że sterownik audio "zgłodnieje" (underrun) i wyda trzask.
- **FFT widma dla podglądu GUI** (duży FFT całego przechwyconego bloku, do
  wykresu widma) nigdy nie może konkurować o czas z powyższymi — to czysto
  kosmetyczny podgląd, może "zgubić klatkę" bez żadnej szkody.

Praktyczna konsekwencja: każdy z tych czterech elementów musi mieć **własny
wątek/proces** i komunikować się z pozostałymi przez kolejki/bufory o
ograniczonym rozmiarze (nigdy przez blokujące wywołanie), tak żeby wolniejszy
konsument nigdy nie zatrzymał szybszego producenta. W szczególności: wątek
odczytujący z SDR nie może w tej samej pętli robić także demodulacji, zapisu
plików ani FFT widma — musi tylko czytać i wrzucać wynik do kolejki.

---

## 2. Abstrakcja sprzętu SDR

### 2.1 Kontrakt "backendu" SDR

Niezależnie od konkretnego sprzętu, backend SDR musi udostępniać dokładnie te
operacje:

- `ustaw_częstotliwość(freq_hz)` — przestraja odbiornik.
- `ustaw_wzmocnienie(tryb, wartość_db)` — tryb to `"auto"` (AGC) lub `"manual"`.
- `czytaj(liczba_próbek) -> tablica zespolonych próbek IQ` — blokuje aż zbierze
  dokładnie tyle próbek ile poproszono (albo rzuca błąd — patrz §2.3).
- `zamknij()`.
- Pole tylko-do-odczytu: zakres wzmocnienia (min, max) w dB, właściwy dla
  danego urządzenia (do ustawienia zakresu suwaka w GUI).

### 2.2 Prawdziwy sprzęt (SoapySDR / RTL-SDR)

- Otwarcie urządzenia po sterowniku (`driver`) i dodatkowych argumentach
  (`device_args`, np. `serial=00000001`), z domyślną przepustowością
  próbkowania **2.4 MSPS** (dopasowaną do RTL-SDR) — to samo ustawienie
  jednocześnie definiuje szerokość jednego okna przechwytu (patrz §5).
- **Bias-tee (zasilanie anteny) musi być zawsze wymuszane na "wyłączone"**,
  bezwarunkowo, przy każdym otwarciu urządzenia i przy każdym przestrojeniu —
  niezależnie od tego, czy użytkownik podał argumenty urządzenia sugerujące
  inaczej. Na RTL-SDR Blog V4 ta sama linia GPIO obsługuje zarówno dawne
  "offset tuning", jak i bias-tee (kompatybilność wsteczna), a jej stan
  utrzymuje się w pamięci chipu między otwarciami procesu — więc jeśli
  poprzedni proces zostawił ją włączoną, nowy proces musi ją jawnie wyłączyć,
  żeby nigdy przypadkiem nie zasilić pasywnej anteny.
- Wzmocnienie: tryb "auto" włącza AGC sprzętu; tryb "manual" ustawia konkretną
  wartość w dB. Zmiana wzmocnienia w trakcie pracy (suwak w GUI) musi być
  odzwierciedlona na sprzęcie **bez restartu skanowania**.
- Rozmiar wewnętrznego bufora strumienia sprzętu powinien być tak dobrany, by
  komfortowo pomieścić czas trwania jednej "działki" próbek (patrz §3.4) plus
  margines (np. ~100 ms bufora przy ~30 ms działce) — zbyt mały bufor
  wymusza więcej pojedynczych wywołań odczytu na jedną działkę, co kosztuje
  dodatkowy narzut na wywołanie.
- Bufor pomocniczy używany do jednego wywołania odczytu ze sprzętu musi być co
  najmniej tak duży jak żądana liczba próbek — inaczej trzeba wykonać drugie
  wywołanie tylko po to, żeby dobrać resztę, co niepotrzebnie podwaja koszt
  narzutu tego wywołania (measured: ~2× czas odczytu przy zbyt małym buforze).
- **Enumeracja urządzeń musi odfiltrować urządzenia typu "audio"** (sterowniki
  eksponujące zwykłe karty dźwiękowe jako rzekome odbiorniki SDR, np. przez
  moduł "audio" biblioteki SoapySDR) — inaczej lista wyboru urządzenia w GUI
  wypełnia się bezużytecznymi kartami dźwiękowymi.
- **Chłodzenie po zamknięciu urządzenia**: jeśli to samo urządzenie USB zostało
  zamknięte przez (potencjalnie inny) proces mniej niż ~3 sekundy temu, nowe
  otwarcie musi poczekać na upłynięcie tych 3 sekund przed próbą otwarcia od
  nowa. Zbyt szybkie ponowne otwarcie tego samego urządzenia USB potrafi
  spowodować crash na poziomie sterownika natywnego (poza kontrolą warstwy
  aplikacji) — czas ostatniego zamknięcia musi być zapamiętany trwale (np. w
  pliku), bo dotyczy to też zamknięcia przez zupełnie inny, wcześniejszy
  proces tej samej aplikacji.

### 2.3 Kontrakt błędu przy braku danych — krytyczny wymóg bezpieczeństwa

Odczyt ze sprzętu, który przez pełną sekundę nie dostarczy pełnej żądanej
liczby próbek (urządzenie odłączone/zawieszone), **musi rzucić błąd**, nigdy nie
zwracać pustej/niepełnej tablicy bez sygnalizacji. Bez tego zabezpieczenia
maszyna stanów skanera (patrz §7) radośnie cyklicznie przechodzi
CALCULATE_NEXT_FREQ → TUNING → SCANNING w nieskończoność na martwym powietrzu —
z zewnątrz wygląda to jak działający skan, mimo że odbiornik jest fizycznie
odłączony. To realny przypadek zaobserwowany podczas rozwoju: użytkownik
odłączył USB w trakcie skanowania, a program "dalej skakał po częstotliwościach"
bez żadnego ostrzeżenia.

### 2.4 Backend symulowany (do testowania bez sprzętu)

Musi istnieć tryb pracy bez fizycznego odbiornika, generujący deterministyczny
sygnał testowy, żeby cały cykl skan→wykrycie→nagranie dał się przetestować bez
sprzętu:

- Szum tła: zespolony szum gaussowski o niskiej amplitudzie.
- Kilka "aktywnych" częstotliwości (np. 121.5, 118.1, 124.0 MHz), z których
  każda cyklicznie "nadaje" fałszywy sygnał AM (ton 800 Hz jako obwiednia) przez
  określony czas (np. 2.5 s) w okresie (np. 5 s).
- **Timing cyklu musi być deterministyczną funkcją czasu zegarowego** (nie
  losową liczbą trwania z zapamiętanym momentem startu) — konkretnie: faza
  cyklu danej "aktywnej" częstotliwości to hash tej częstotliwości modulo okres,
  dodany do bieżącego czasu. Dzięki temu przestrojenie się z powrotem na tę samą
  częstotliwość **nie może** przypadkowo "anulować" zaplanowanego wybuchu emisji.
- Sygnał musi pojawić się na poprawnym przesunięciu częstotliwości względem
  aktualnie nastrojonego środka okna — w trybie szerokopasmowym środek okna
  praktycznie nigdy nie pokrywa się dokładnie z żadną "aktywną" częstotliwością
  (patrz §5.1, celowe przesunięcie środka), więc dopasowanie musi sprawdzać
  każdą aktywną częstotliwość względem bieżącej szerokości przechwytu, nie
  tylko dokładne trafienie.
- Odczyt musi "usypiać" na czas realny odpowiadający żądanej liczbie próbek
  (jak prawdziwe urządzenie blokujące na I/O) — inaczej pętla silnika kręci się
  tysiące razy na sekundę i zalewa GUI zdarzeniami.
- Zmiana wzmocnienia musi widocznie skalować amplitudę fałszywego sygnału
  (żeby suwak Gain robił coś zauważalnego nawet bez sprzętu).

### 2.5 Wątek akwizycji — rozdzielenie odczytu sprzętu od reszty przetwarzania

Prawdziwy odczyt ze sprzętu musi działać na **własnym, dedykowanym wątku**,
którego jedynym zadaniem jest wołanie odczytu sprzętu w pętli i wrzucanie
wyniku do ograniczonej kolejki FIFO (np. maks. 64 elementy — przy typowym
rozmiarze działki to kilka sekund zapasu). Główna pętla skanera staje się
*konsumentem* tej kolejki, nie woła sprzętu bezpośrednio.

Zasady:

- **Tylko ten jeden wątek** może kiedykolwiek dotykać sprzętu — zarówno odczyt,
  jak i zmianę częstotliwości/wzmocnienia. Żądania zmiany częstotliwości/
  wzmocnienia z wątku-konsumenta muszą być przekazywane jako **asynchroniczne
  polecenie** (nie przez współdzieloną blokadę/mutex) — wątek akwizycji
  sprawdza pod kątem oczekującego polecenia na początku każdej iteracji pętli,
  wykonuje je (jeśli jest), po czym dopiero robi kolejny odczyt sprzętu.
  Wywołujący (konsument) blokuje się do potwierdzenia wykonania polecenia
  (ograniczone czasowo — jeśli wątek akwizycji nie odpowie w rozsądnym czasie,
  to błąd identyczny jak martwe urządzenie).
- **Dlaczego nie zwykła współdzielona blokada**: jeśli wątek akwizycji ma
  podniesiony priorytet czasu rzeczywistego (patrz §2.6), a blokadę współdzieli
  z wątkiem o normalnym priorytecie, dochodzi do *odwrócenia priorytetów* —
  wątek RT wygrywa wyścig o ponowne zajęcie blokady niemal za każdym razem, gdy
  ją zwalnia, głodząc wątek normalny na sekundy (zmierzone: realnie do 11.7
  sekundy). Model "jeden właściciel + kolejka poleceń" eliminuje tę klasę błędu
  z definicji — nie ma drugiego wątku, który mógłby zostać zagłodzony o tę samą
  blokadę.
- Licznik "generacji": każda faktycznie wykonana zmiana częstotliwości/
  wzmocnienia zwiększa licznik generacji i czyści kolejkę. Blok próbek, którego
  odczyt rozpoczął się *przed* zmianą, ale zakończył *po* niej, musi zostać
  **odrzucony** (nie trafić do kolejki) — inaczej pierwszy blok nowego okna
  może w rzeczywistości być resztką poprzedniego okna, co fałszywie podnosi
  odczytaną moc i wywołuje fałszywe wykrycia sygnału (potwierdzone na prawdziwym
  sprzęcie).
- Odczyt konsumenta blokuje się do pojawienia się danych w kolejce (z
  timeoutem, np. 1.5 s) i propaguje dowolny błąd zgłoszony przez wątek
  akwizycji (w tym błąd z §2.3) — istniejąca logika ponownego łączenia (§2.7)
  działa więc bez zmian, niezależnie od tego pośredniego wątku.

### 2.6 Priorytet czasu rzeczywistego (best-effort)

Zarówno wątek akwizycji, jak i główny wątek skanera powinny (jeśli system na to
pozwala — np. Linux z uprawnieniami/limitami RT) prosić system operacyjny o
planowanie czasu rzeczywistego (SCHED_FIFO), tą samą techniką, której używają
serwery audio czasu rzeczywistego (JACK, PipeWire). To poprawia płynność audio
na żywo, ale **nigdy nie jest wymogiem twardym** — brak uprawnień musi być
cicho ignorowany, program działa identycznie funkcjonalnie, tylko z gorszą
odpornością na jitter.

### 2.7 Automatyczne ponowne łączenie

Utrata połączenia ze sprzętem (błąd z odczytu, patrz §2.3) **nie zatrzymuje**
skanowania — silnik musi:

1. Zamknąć wszystkie aktualnie otwarte transmisje/nagrania tak, jakby
   naturalnie się skończyły (transmisja "jest definitywnie skończona").
2. Zamknąć backend.
3. Próbować otworzyć go ponownie w pętli, ze stałym opóźnieniem między próbami
   (np. 2 s — celowo krótkim i stałym, nie rosnącym wykładniczo, żeby
   użytkownik mógł kilka razy z rzędu odłączyć/podłączyć dongle USB i szybko
   trafić w kolejną próbę), aż się uda albo użytkownik naciśnie Stop.
4. W trakcie prób ponownego łączenia GUI musi pokazywać osobny stan
   "RECONNECTING", odróżnialny od normalnego skanowania.
5. **Jeśli w tej sesji już raz z powodzeniem otwarto prawdziwy sprzęt** (nie
   symulację), kolejne próby ponownego otwarcia muszą celować **wyłącznie w
   prawdziwe urządzenie**, nigdy nie może nastąpić ciche przełączenie na
   backend symulowany tylko dlatego, że urządzenie chwilowo nie odpowiada.
   Inaczej odłączony na stałe dongle wygląda z zewnątrz jak dalej "działający"
   skan (tyle że na fałszywych danych) — dokładnie ten sam mylący objaw, któremu
   zapobiega §2.3.
6. Natomiast **przy pierwszym uruchomieniu** (jeszcze żadne urządzenie się nie
   otworzyło), jeśli użytkownik wybrał tryb "automatyczny" (nie konkretny
   sterownik), brak sprzętu powinien cicho przełączyć na symulację — to ma być
   wygodny tryb demo/testowy, nie mylący błąd. Jeśli użytkownik **jawnie
   wybrał konkretne urządzenie** i ono nie otwiera się w ogóle, błąd musi być
   głośno zasygnalizowany, nigdy nie zastąpiony cicho fałszywymi danymi.

---

## 3. Konfiguracja i trwałość danych

Cała konfiguracja jest trwale zapisywana (np. do pliku JSON w katalogu
konfiguracyjnym użytkownika) i automatycznie wczytywana przy starcie, tak żeby
program nigdy nie wymagał ponownej konfiguracji po restarcie.

### 3.1 Ustawienia urządzenia

| Ustawienie | Domyślna wartość | Opis |
|---|---|---|
| Sterownik (driver) | `"auto"` | `"auto"`, `"simulated"`, albo konkretny sterownik SoapySDR (np. `"rtlsdr"`) |
| Argumenty urządzenia | `""` | np. `serial=00000001` |
| Przepustowość próbkowania | 2 400 000 Hz | podwójna rola: także szerokość jednego okna przechwytu (§5) |
| Tryb wzmocnienia | `"auto"` | `"auto"` (AGC) lub `"manual"` |
| Wzmocnienie (manual) | 30.0 dB | zakres z urządzenia (domyślnie 0–50 dB zanim urządzenie zostanie otwarte) |
| Wyjście audio | domyślne systemowe | indeks urządzenia PortAudio, -1 = domyślne |

### 3.2 Pasma/zakresy do skanowania

Lista pozycji (pasm), każda z polami:

- Zakres częstotliwości: start_hz, stop_hz, krok_hz (start==stop → pojedyncza
  częstotliwość, np. 121.500 MHz guard).
- Etykieta (nazwa wyświetlana).
- Włączony/wyłączony (checkbox — pozostaje na liście, ale pomijany przy
  skanowaniu).
- Modulacja: AM / NFM / WFM.
- Waga priorytetu (domyślnie 0.0, patrz §6).
- Opcjonalne "zaawansowane" nadpisania per-pasmo (wszystkie domyślnie `None` =
  "użyj globalnego ustawienia"): próg wyzwalania (dBFS), czas przestrajania
  (ms), czas trzymania/linger (ms), tryb i wartość wzmocnienia, włączenie i
  siła redukcji szumów.

Domyślna zawartość listy przy pierwszym uruchomieniu: jedno pasmo "Lotnictwo
cywilne", 118.000–136.975 MHz, krok 8.333 kHz, modulacja AM.

Gotowe presety pasm do jednoklikowego dodania (patrz §14.3): lotnictwo cywilne,
VHF wojskowe, UHF wojskowe, PMR446, pasmo amatorskie 2 m i 70 cm, VHF morskie,
radio FM (WFM), VHF straży pożarnej, PRM.

### 3.3 Lista pamięci (Memory List) — pojedyncze kanały

Niezależna od pasm lista pojedynczych kanałów, każdy z: częstotliwością,
nazwą, modulacją, flagą włączony/wyłączony, folderem (do grupowania — pusty =
"bez folderu"), wagą priorytetu (jak wyżej). Ma własny **globalny przełącznik
włącz/wyłącz całą listę** (niezależny od flag pojedynczych kanałów — wyłączenie
tu wyłącza wszystkie kanały naraz, nawet te oznaczone jako "włączone"
indywidualnie).

**Pasma i Lista pamięci są zawsze skanowane razem, w jednej sesji** — nie ma
osobnego "trybu" przełączania między nimi. Zapisywana osobno do pliku CSV
(`freq_hz,name,modulation,enabled,folder,weight`), tolerancyjnie na
uszkodzone/niekompletne wiersze (pomija z ostrzeżeniem zamiast się wywalić).

### 3.4 Czasy skanowania

| Ustawienie | Domyślna wartość | Zakres w GUI |
|---|---|---|
| Czas przestrajania (Tuning Time) | 60 ms | 0–5000 ms |
| Czas trzymania (Linger Time) | 1500 ms | 0–30000 ms |
| Czas nasłuchu na oknie (Scanning Dwell) | 45 ms | (bez suwaka w GUI) |
| Rozmiar "działki" próbek (chunk) | 30 ms realnego czasu (przy bieżącej częstotliwości próbkowania) | stała |

### 3.5 Nagrywanie

| Ustawienie | Domyślna wartość |
|---|---|
| Włączone | tak |
| Tryb | `"split"` (osobny plik na transmisję) lub `"single"` (jeden ciągły plik sesji) |
| Katalog docelowy | `~/SmartScanATC/recordings` |
| Wstawiaj znacznik dźwiękowy między transmisjami (tylko tryb single) | tak |

### 3.6 Próg wyzwalania i squelch

| Ustawienie | Domyślna wartość | Zakres |
|---|---|---|
| Próg wyzwalania (Trigger Level) | −30.0 dBFS | −90 do 0 dBFS |
| Squelch włączony | nie | — |
| Offset squelcha od progu | −6.0 dB | −40 do 0 dB (zawsze ≤ 0) |

Pełna semantyka squelcha — patrz §9.

### 3.7 Recency boost ("dogrywanie" świeżo zakończonych transmisji)

| Ustawienie | Domyślna wartość |
|---|---|
| Włączone | tak |
| Czas trwania boostu po zakończeniu transmisji | 90 s |
| Odstęp między dodatkowymi wizytami w trakcie boostu | 8 s |

Pełny opis — patrz §6.5.

### 3.8 Redukcja szumów

| Ustawienie | Domyślna wartość | Zakres |
|---|---|---|
| Włączona | nie | — |
| Siła | 0.6 (60%) | 0–100% w GUI, 0.0–1.0 wewnętrznie |

### 3.9 Stereo FM

`wfm_stereo_enabled` (domyślnie: nie) — dekodowanie stereo dla WFM **tylko na
żywo**; nagrania zawsze mono niezależnie od tego ustawienia.

### 3.10 Transkrypcja

| Ustawienie | Domyślna wartość |
|---|---|
| Włączona (automatyczna kolejka w tle) | tak |
| Model ogólny (wielojęzyczny) | `large-v3` |
| Model angielski dostrojony do ATC | `jacktol/whisper-medium.en-fine-tuned-for-ATC-faster-whisper` |
| Urządzenie | `"auto"` (GPU jeśli dostępne, inaczej CPU) |
| Typ obliczeń | `"auto"` |
| Kandydujące języki | `["en", "pl"]` |
| Minimalny czas trwania do transkrypcji | 0.6 s |

### 3.11 Lokalizacja odbiornika

`home_lat`/`home_lon` (domyślnie 52.123, 21.012) — używane do budowania
linków playbacku FlightRadar24/ADSBExchange (§17.5).

### 3.12 Inne ustawienia globalne

- Domyślny margines wpisu na czarnej liście: 5000 Hz.
- Szerokość okna mocy per-kanał w trybie Listy pamięci: 12 500 Hz (w trybie
  pasm zamiast tego używany jest krok danego pasma).
- Znane nazwy folderów Listy pamięci (żeby puste foldery nie znikały z drzewa).

### 3.13 Pliki na dysku

- Konfiguracja: `config.json` w katalogu konfiguracyjnym (np.
  `~/.config/SmartScanATC/`).
- `channels.csv`, `blacklist.csv` w tym samym katalogu.
- Nagrania: `<output_dir>/<RRRR-MM-DD>/<znacznik_czasu>_<kHz>.wav` w trybie
  split (patrz §11.3); pojedynczy plik sesji w trybie single.
- Pamięć podręczna jakości nagrań, pamięć podręczna transkrypcji, lista
  ulubionych — osobne pliki JSON w katalogu danych aplikacji.
- Dziennik kalibracji mocy (`power_calibration.jsonl`) i pochodny z niego cache
  krzywej kompensacji brzegowej (§8) — w katalogu konfiguracyjnym.

---

## 4. Model danych

### 4.1 Kanał (Channel)

`freq_hz, name, modulation (AM/NFM/WFM), enabled, folder, weight`.

### 4.2 Wpis czarnej listy (BlacklistEntry)

`freq_hz, margin_hz (domyślnie 5000), label, date_added`. Częstotliwość jest
zablokowana, jeśli `|f − freq_hz| ≤ margin_hz`.

### 4.3 Stan skanera (do wyświetlenia w GUI)

`IDLE`, `CALCULATE_NEXT_FREQ`, `TUNING`, `SCANNING`, `SIGNAL_DETECTED`,
`LINGER`, `RECONNECTING`. Tylko `CALCULATE_NEXT_FREQ`/`TUNING`/`SCANNING`
sterują realnie pętlą silnika (patrz §7) — `SIGNAL_DETECTED`/`LINGER` to
kosmetyczny stan pochodny, wyliczany z tego, czy jakikolwiek "slot" (§7.3) jest
aktualnie otwarty i czy ma ustawiony licznik trzymania.

---

## 5. Kanalizacja szerokopasmowa (wideband channelization)

To mechanizm pozwalający nasłuchiwać dziesiątek/setek kanałów jednocześnie bez
przestrajania się kanał-po-kanale.

### 5.1 Planowanie okien przechwytu

Dany jest posortowany zbiór unikalnych częstotliwości kanałów do obserwowania,
przepustowość próbkowania (np. 2.4 MHz) i szerokość kanału (np. krok pasma albo
szerokość okna mocy Listy pamięci). Algorytm zachłanny:

1. Uznaj za "użyteczną" tylko `USABLE_FRACTION = 90%` całej przepustowości
   próbkowania (bo tunery SDR mają spadek czułości blisko krawędzi pasma
   przechwytu i mogą mieć fałszywy prążek DC/LO dokładnie na środku).
2. Idź po posortowanych częstotliwościach; dołączaj kolejną do bieżącej grupy,
   dopóki rozpiętość grupy (od najniższej do najnowszej) plus szerokość kanału
   mieści się w użytecznej przepustowości.
3. Gdy się nie mieści — zamknij bieżącą grupę jako jedno "okno", zacznij nową
   od bieżącej częstotliwości.
4. Środek okna = środek zakresu grupy, **celowo przesunięty o
   `0.37 × szerokość_kanału`** od dokładnego środka siatki, żeby żaden kanał
   nie wylądował dokładnie na środku okna (tam bywa prążek DC/LO).

Efekt: cały skanowany zakres (np. 118–136.975 MHz co 8.33 kHz) zwykle zwija się
w garstkę okien 2.4 MHz — często jedno, jeśli lista pamięci jest wąska — a
tuner po rozpoczęciu skanu może w ogóle nie musieć się już ruszać.

### 5.2 Rozszerzanie krawędzi okna (opcjonalne, "wyjrzyj poza pasmo")

Skrajne (najniższe i najwyższe częstotliwościowo) okna danej "linii"
skanowania (pasma) mogą być poszerzone o kilka dodatkowych częstotliwości
próbkowych *tuż poza* skonfigurowanym zakresem — tak żeby sygnał leżący
dokładnie na granicy skonfigurowanego pasma miał większą szansę zostać złapany
zamiast wpaść w martwą strefę zostawioną przez margines 90%. Nigdy nie zmienia
to środka okna (nie wymaga dodatkowego przestrojenia) — wykorzystuje tylko
zapas miejsca, który i tak już zostawia margines `USABLE_FRACTION`, ograniczony
dodatkowo do maks. 15% całej przepustowości próbkowania, żeby wąskie/prawie
puste okno nie zaczęło ponownie zamiatać większości reszty przechwytu.

### 5.3 Pomiar mocy per-kanał (wykrywanie sygnału)

Dla każdego przechwyconego bloku próbek:

1. Policz **jedno** FFT całego bloku (z oknem Hanna), zamień na gęstość
   widmową mocy (PSD), znormalizowaną przez sumę kwadratów okna Hanna i liczbę
   próbek.
2. Zbuduj **skumulowaną sumę** (cumsum) tej PSD wzdłuż osi częstotliwości —
   pozwala to odczytać sumę mocy w dowolnym zakresie binów jako różnicę dwóch
   wartości cumsum, bez ponownego sumowania.
3. Dla każdej sprawdzanej częstotliwości: policz jej przesunięcie od środka
   okna, znajdź (przez wyszukiwanie binarne w posortowanej osi częstotliwości
   binów FFT) indeksy dolnej/górnej krawędzi jej pasma (offset ± połowa
   szerokości kanału), odczytaj moc jako `cumsum[górny] − cumsum[dolny]`.
4. Wynik w dB: `10·log₁₀(moc)` (moc podłogowana do minimum ~1e-12, żeby
   uniknąć log(0)).

To sprawia, że koszt sprawdzenia N kanałów w jednym przechwyconym bloku to
jedno FFT + O(N log n) (wyszukiwanie binarne), a nie N osobnych FFT/demodulacji
ani liniowe przeszukiwanie wszystkich binów FFT dla każdego kanału osobno — to
jest to, co realnie pozwala obserwować setki/tysiące kanałów naraz w czasie
rzeczywistym.

**Jeden i ten sam wynik FFT/PSD/cumsum tego bloku musi być wielokrotnie użyty**
w tym samym takcie: raz do sprawdzenia mocy już aktywnych transmisji, raz do
sprawdzenia kandydatów na nowe wykrycia — nigdy nie liczony od nowa dla
każdego z tych dwóch celów osobno.

### 5.4 Podgląd widma (GUI)

Osobna, tańsza redukcja tego samego FFT do ustalonej liczby punktów (np. 512)
do wykresu w GUI: każdy punkt wyświetlany to **suma** (nie średnia!) mocy PSD
grupy binów, które reprezentuje — sumowanie odzwierciedla rzeczywistą moc
zintegrowaną po paśmie (ta sama konwencja co przy odczycie mocy kanału wyżej),
podczas gdy uśrednianie zaniżałoby wynik o wartość zależną od liczby binów na
punkt (w praktyce o ponad 20 dB przy typowych parametrach) i myliłoby
użytkownika liniami progu wyzwalania i widma rysowanymi na tej samej osi, ale
niebędącymi w rzeczywistości tą samą wielkością fizyczną.

---

## 6. Harmonogramowanie skanowania (ważone round-robin)

### 6.1 Podział na "linie" (lanes)

Wszystko co ma wagę priorytetu 0 (domyślnie) — pasma i kanały Listy pamięci —
trafia do jednej wspólnej **linii domyślnej**, skanowanej w zwykłej kolejności
round-robin (dokładnie jak przed wprowadzeniem wag).

Wszystko z wagą > 0 staje się osobną **linią priorytetową**:

- **Linia zasięgu** — całe pasmo z wagą, przemiatające własną siatkę okien we
  własnej wewnętrznej rotacji między kolejnymi turami.
- **Linia punktowa** — jeden lub kilka blisko leżących ważonych kanałów Listy
  pamięci, złączonych w jedno okno przechwytu, jeśli mieszczą się razem
  (korzyść wspólnej wizyty przeważa nad wadami — nie ma powodu ich rozdzielać).

### 6.2 Ważony round-robin (smooth weighted round-robin)

Ten sam algorytm, którego używa nginx do rozkładania obciążenia na serwery z
wagami: każda linia ma licznik "kredytu". Przy każdym wyborze:

1. Do kredytu każdej linii dodaj jej wagę.
2. Wybierz linię z najwyższym kredytem.
3. Odejmij od jej kredytu sumę wszystkich wag.

Efekt: linia z dwa razy większą wagą niż inna jest wybierana dwa razy częściej,
**równomiernie przeplatana**, a nie w seriach — wagi (1,1) dają np. A,B,A,B,…, a
wagi (2,1) dają w przybliżeniu A,B,A,A,B,A,…

### 6.3 Kiedy dostaje turę linia domyślna (waga 0)

Linia domyślna dostaje swoją turę za każdym razem, gdy jakaś **linia zasięgu**
kończy pełne okrążenie własnych okien. Jeśli w ogóle nie ma linii zasięgu
(tylko linie punktowe), linia domyślna dostaje turę raz na pełny cykl całego
ważonego schedulera (suma wag). To sprawia, że pozycje z wagą 0 "czekają na
swoją kolej", ale i tak są regularnie odwiedzane, zamiast czekać w
nieskończoność, gdyby coś ważonego istniało.

### 6.4 "Szerokie zakotwiczenie" ważonego kanału wewnątrz aktywnie skanowanego pasma

Jeśli ważony kanał Listy pamięci leży wewnątrz zakresu jakiegoś (dowolnie
ważonego) aktywnie skanowanego pasma, jego wizyta priorytetowa **nie jest
wąskim oknem tylko na ten jeden kanał** — zamiast tego generowane są dwa pełne
okna przechwytu o szerokości całej przepustowości próbkowania, jedno
zakotwiczone blisko lewej krawędzi grupy kanałów, drugie blisko prawej,
wypełnione resztą normalnej siatki tego pasma. Na przemian używane jest raz
jedno, raz drugie zakotwiczenie (żeby przy każdej wizycie omieść świeże
sąsiedztwo zamiast zawsze to samo). Sens: priorytetowa wizyta "przy okazji"
daje darmowe pokrycie sąsiednich kanałów pasma — wykrycia obok wciąż działają
normalnie, i samo pasmo nie traci nic ze swojego zwykłego zasięgu, tylko rzadziej
musi dedykować mu osobną turę.

### 6.5 Recency boost — częstsze odwiedzanie świeżo zakończonej transmisji

Gdy transmisja **kończy się naturalnie** (upływa czas trzymania/linger, level
spadł poniżej progu i już nie wrócił — **nie** ręczny Skip ani zatrzymanie
skanera ani utrata połączenia), jej okno dostaje tymczasowy "boost":

- Przez `duration_s` sekund (domyślnie 90 s) od zakończenia, to okno jest
  dodatkowo odwiedzane co `interval_s` sekund (domyślnie 8 s) — **ponad**
  zwykły harmonogram, nie zamiast niego.
- Sprawdzenie "czy boost jest już należny" ma pierwszeństwo przed zwykłym
  ważonym wyborem, ale to zawsze tylko **jedna dodatkowa wizyta wstawiona w
  kolejkę**, nigdy przejęcie na stałe — następny wybór wraca normalnie do
  zwykłego harmonogramu, więc nic innego nie czeka dłużej z tego powodu.
- Uzasadnienie: kolejna transmisja tej samej rozmowy (typowa wymiana ATC)
  często ląduje w ciągu następnych kilku/kilkunastu-kilkudziesięciu sekund —
  ale w przeciwieństwie do samego podniesienia czasu trzymania, to nigdy nie
  blokuje zauważenia, że rozmowa przeniosła się na inną częstotliwość w
  międzyczasie.
- Rejestracja boostu jest zawsze zgodna z **bieżącym** ustawieniem
  `recency_boost.enabled` w momencie zamknięcia transmisji (nie z ustawieniem
  sprzed przebudowy harmonogramu) — przełączenie w Opcjach działa od razu, na
  następnej zakończonej transmisji, bez potrzeby restartu skanu.

### 6.6 Tryb "solo" jednego pasma (edycja zaawansowana)

Kiedy użytkownik edytuje "zaawansowane" ustawienia jednego konkretnego pasma
(patrz §3.2, §14.1), skaner może zostać ograniczony **wyłącznie** do tego
jednego pasma — wszystkie inne pasma i cała Lista pamięci są tymczasowo
wyłączone z rywalizacji o czas, żeby dostrajanie triggera/timingu/wzmocnienia/
redukcji szumów dla tego pasma dało się słyszeć/widzieć w izolacji, na żywo.
Wyjście z trybu edycji przywraca normalny, pełny harmonogram.

---

## 7. Maszyna stanów skanera

### 7.1 Trzy stany sterujące pętlą

1. **CALCULATE_NEXT_FREQ** — wybierz następne okno przechwytu (§6). Jeśli
   wybrane okno to dokładnie to, na którym tuner już stoi (np. wąskie pasmo
   mieszczące się w jednym oknie, albo tryb solo), pomiń przestrajanie i
   przeskocz od razu do SCANNING — realne przestrojenie plus czekanie na
   ustabilizowanie się tunera byłoby bezcelowe i fałszywie zaraportowałoby
   "powrót do tego okna" dla okna, którego w rzeczywistości nigdy nie
   opuszczono.
2. **TUNING** — przestrój odbiornik na środek wybranego okna, odczekaj
   skonfigurowany "czas przestrajania" (odrzucając odczytane w tym czasie
   próbki — głowa jest wyciszona, bo tuner się jeszcze stabilizuje), po czym
   przejdź do SCANNING.
3. **SCANNING** — odczytaj kolejną "działkę" próbek i dla każdej:
   - Policz FFT/PSD raz na cały blok (§5.3).
   - **Obsłuż wszystkie aktualnie otwarte "sloty"** (aktywne transmisje, §7.3) —
     zdemoduluj każdą, zdecyduj o kontynuacji/trzymaniu/zamknięciu.
   - **Poszukaj nowej aktywności** wśród częstotliwości okna, które nie są
     jeszcze aktywne i nie są zablokowane na czarnej liście — znajdź
     najsilniejszą po skorygowaniu o kompensację brzegową (§8).
   - Jeśli poproszono o Skip/Skip+Blacklist — zamknij wszystkie aktywne sloty
     (ewentualnie blokując ich częstotliwości) i wróć do CALCULATE_NEXT_FREQ.
   - W przeciwnym razie: jeśli najsilniejszy kandydat przekracza efektywny próg
     wyzwalania (§7.4) — otwórz dla niego nowy slot.
   - Jeśli nic nie jest aktywne i minął czas nasłuchu na tym oknie — wróć do
     CALCULATE_NEXT_FREQ (opcjonalnie zapisując migawkę tła do kalibracji, §8.2).
     **Dopóki cokolwiek jest aktywne, okno nie jest opuszczane** — czas
     nasłuchu nie ma znaczenia, gdy trwa transmisja.

### 7.2 Wykrywanie sygnału — próg i korekcja brzegowa

Moc każdego kandydata jest **skorygowana** o kompensację spadku czułości
blisko krawędzi okna (§8) tylko na potrzeby decyzji o wyzwoleniu/trzymaniu —
surowa (nieskorygowana) wartość jest zawsze zachowywana osobno do logowania
kalibracji i do metadanych nagrania (żeby korekcja nigdy nie zasilała sama
siebie własnymi, już skorygowanymi danymi).

Najsilniejszy kandydat wyzwala nowy slot, jeśli jego skorygowana moc ≥
**efektywny próg wyzwalania tego okna** — próg globalny, chyba że okno
pochodzi z pasma mającego własne "zaawansowane" nadpisanie progu (wtedy to
nadpisanie wygrywa).

### 7.3 Sloty — wiele równoczesnych transmisji w jednym oknie

Skoro okno i tak jednocześnie obserwuje wiele kanałów pod kątem wykrycia,
równie dobrze może **jednocześnie obsługiwać wiele aktywnych transmisji**, nie
tylko blokować się na jednej najsilniejszej i ignorować resztę okna aż się
skończy. Nie ma sztywnego limitu liczby jednocześnie otwartych slotów (ograniczone
tylko tym, ile kanałów faktycznie grupuje jedno okno — a to i tak jest
utrzymywane w rozsądnych granicach przez planowanie okien, §5.1).

Każdy slot ma **własny, niezależny** stan demodulatora, redukcji szumów i
nagrywania — dwa jednocześnie aktywne kanały nigdy nie dzielą stanu filtrów.
Otwarcie slotu:

- Tworzy nowy demodulator (ze stanem: DC-estymator AM, faza FM, ...), nową
  instancję redukcji szumów, nowy rekorder.
- Zapisuje częstotliwość, nazwę (jeśli znana z Listy pamieci — patrz niżej),
  modulację, przesunięcie od środka okna.
- Rozstrzyga, który slot dostaje **żywy głośnik**: wygrywa wyższa waga
  priorytetu kanału; remis zostawia dotychczasowego posiadacza (kto pierwszy
  usłyszany, ten trzyma, dopóki coś ściśle wyżej ważonego się nie pojawi).
- Zapisuje pełny zestaw metadanych diagnostycznych (do osadzenia w pliku WAV
  po zamknięciu, patrz §11.4): częstotliwość, nazwa, modulacja, waga, środek
  okna, przesunięcie, przepustowość próbkowania, szerokość kanału, próg
  wyzwalania, zmierzona moc przy wyzwoleniu, korekcja brzegowa, tryb/wartość
  wzmocnienia, sterownik urządzenia, czasy przestrajania/nasłuchu/trzymania,
  status redukcji szumów, znacznik czasu UTC otwarcia.
- Jeśli nagrywanie włączone: rozpoczyna sesję nagrywania (jeśli tryb
  pojedynczego pliku i sesja jeszcze nie trwa) i rozpoczyna nagrywanie tej
  transmisji (nowy plik w trybie split, albo znacznik dźwiękowy w trybie
  single jeśli plik już trwa).

**Rozpoznawanie nazwy kanału do wyświetlenia** działa dwustopniowo: najpierw
dokładne dopasowanie częstotliwości (tylko dla kanałów faktycznie
uwzględnionych w harmonogramie), a jeśli brak — dopasowanie po zaokrągleniu do
kHz do *dowolnego* nazwanego kanału z Listy pamięci, niezależnie od tego, czy
jest on aktualnie włączony. Powód: kanał może zostać przypadkiem "złapany"
przez szerokie przemiatanie pasma mimo bycia wyłączonym — a odtwarzacz i tak
pokazuje nazwę takich nagrań po zaokrąglonej częstotliwości, więc nasłuch na
żywo powinien być z tym spójny.

Zamknięcie slotu (§7.5) — z powodu: naturalnego wygaśnięcia trzymania
(`"linger"`), ręcznego Skip (`"skip"`), Skip+Blacklist (`"blacklist_skip"`),
zatrzymania skanera (`"stopped"`) lub utraty połączenia (`"connection_lost"`).
Tylko powód `"linger"` rejestruje recency boost (§6.5).

### 7.4 Efektywne wartości per-okno (nadpisania "zaawansowane" pasma)

Dla każdego z: progu wyzwalania, czasu przestrajania, czasu trzymania,
trybu/wartości wzmocnienia, włączenia/siły redukcji szumów — jeśli okno
pochodzi z pasma mającego jawnie ustawione (nie `None`) własne nadpisanie tej
wartości, używane jest ono; inaczej używana jest globalna wartość ze
Skanera/Opcji. Okno **niezwiązane** z jednym konkretnym pasmem (np. mieszające
kanały z dwóch różnych pasm w jednym przechwycie, albo z puli domyślnej)
zawsze używa globalnych ustawień — nigdy nie zgaduje, którego z dwóch
nadpisań użyć.

### 7.5 Czas trzymania (LINGER) — per slot, nie globalnie

Każdy slot ma własny "termin trzymania" (`linger_deadline`):

- `None`, dopóki poziom sygnału tego kanału jest ≥ efektywny próg wyzwalania
  (transmisja "trwa" — realnie stan SIGNAL_DETECTED).
- W chwili, gdy poziom spada poniżej progu, termin jest ustawiany na
  `teraz + efektywny_czas_trzymania`.
- Jeśli poziom wróci ≥ próg przed upłynięciem terminu — termin jest znów
  czyszczony na `None` (transmisja "wznowiła się").
- Jeśli termin upłynie zanim poziom wróci — slot jest zamykany (powód
  `"linger"`).

To dokładnie odwzorowuje dawny globalny stan SIGNAL_DETECTED/LINGER, tylko
śledzone niezależnie dla każdego slotu zamiast jako jeden wspólny stan silnika.

---

## 8. Kompensacja spadku czułości na krawędziach okna (edge compensation)

Tunery SDR mają mniejszą czułość blisko krawędzi przechwyconego pasma niż w
środku — to sprawia, że ten sam absolutny próg wyzwalania jest efektywnie
bardziej rygorystyczny dla kanału blisko krawędzi okna niż blisko środka.

### 8.1 Zbieranie danych kalibracyjnych

Kiedy okno kończy swój czas nasłuchu **bez żadnego wykrycia** (a więc mierzony
poziom to naprawdę tło, nieskażone realną transmisją), zapisz migawkę mocy
każdego obserwowanego kanału w tym oknie, pogrupowaną w kubełki po
przesunięciu od środka okna (np. co 100 kHz), uśrednioną w domenie mocy
liniowej (nie w dB — wartości w dB nie uśredniają się poprawnie). Rzucane co
najwyżej raz na X sekund (np. 30 s) na to samo okno — nie warto płacić kosztu
dodatkowego pełnego pomiaru mocy całego okna co takt.

### 8.2 Budowanie krzywej korekcji

Przy starcie silnika, z całej dotychczas nagromadzonej historii (pula danych
niezależna od konkretnego pasma/okna — fizyczna przyczyna spadku czułości to
własność samego tunera względem miejsca, na które jest aktualnie nastrojony,
nie własność konkretnej częstotliwości):

1. Dla każdego kubełka policz średnią moc (w domenie liniowej), tylko jeśli ma
   wystarczająco dużo próbek (np. ≥ 20 — mniej jest zbyt szumiące, żeby
   ufać).
2. Znajdź kubełek o najwyższej średniej mocy — to punkt odniesienia
   (niekoniecznie dokładnie offset 0).
3. Korekcja dla każdego kubełka = `max(0, punkt_odniesienia − jego_średnia)`,
   ograniczona z góry (np. do 10 dB — realny spadek w użytecznym 90% oknie
   przechwytu nigdy nie sięgał więcej w praktyce; ogranicznik chroni przed
   przesadną korektą pojedynczego martwego/źle skonfigurowanego kubełka).

Bez żadnych danych jeszcze (świeża instalacja) każda korekcja wynosi 0 dB —
identycznie jak zachowanie sprzed wprowadzenia tego mechanizmu, nigdy regresja.

Korekcja jest **jednorazowo wczytana przy starcie silnika**, nie odświeżana w
trakcie sesji — spadek czułości to stabilna właściwość fizyczna tunera.

### 8.3 Wydajne, przyrostowe parsowanie dziennika kalibracji

Surowy dziennik (jeden wiersz JSON na zdarzenie) może urosnąć do wielu
megabajtów przez wiele sesji. Budowanie krzywej **nie może** za każdym razem
parsować całego pliku od nowa — musi trzymać (w osobnym pliku pamięci
podręcznej) sumy/liczniki per-kubełek już zsumowane wcześniej oraz liczbę
bajtów pliku już przetworzonych, i przy każdym starcie doczytywać tylko **nowo
dopisane** bajty od tego miejsca. Jeśli plik dziennika skurczył się (np. ktoś
go ręcznie wyczyścił) w stosunku do zapamiętanej liczby przetworzonych bajtów —
zacznij liczyć od zera zamiast próbować czytać poza koniec pliku.

Dziennik jest ograniczony do maksymalnego rozmiaru (np. 50 MB) — po
przekroczeniu, logowanie po prostu **cicho przestaje** dopisywać nowe wiersze
do końca bieżącej sesji, zamiast zapełniać dysk w nieskończoność.

---

## 9. Squelch — wyciszanie szumu progowego FM

### 9.1 Problem, który to rozwiązuje

Demodulator FM (pochodna fazy) karmiony czystym szumem odbiornika (gdy realna
nośna właśnie zniknęła po puszczeniu PTT nadajnika) produkuje głośny,
szerokopasmowy szum wyjściowy mimo niskiej realnej mocy RF — to fizyka
demodulacji FM poniżej progu ("efekt progowy FM"), nie usterka. Bez żadnego
dodatkowego mechanizmu ten szum jest słyszalny/nagrywany przez cały czas
trzymania (LINGER), aż poziom minie termin trzymania.

### 9.2 Niezmiennik bezpieczeństwa — dlaczego to nigdy nie powtarza znanego błędu SDR++

Squelch **nigdy nie jest niezależnym progiem absolutnym**. Jest wyrażony
zawsze jako offset w dB (zawsze ≤ 0) **od tego samego efektywnego progu
wyzwalania**, którego używa maszyna stanów do decyzji SIGNAL_DETECTED/LINGER:

```
efektywny_próg_squelcha(okno) = efektywny_próg_wyzwalania(okno) + min(0, offset_squelcha_db)
```

Ponieważ offset jest zawsze ≤ 0, próg squelcha **nigdy nie może znaleźć się
powyżej** progu wyzwalania — a zatem squelch **nigdy nie może zadziałać, gdy
poziom sygnału jest wciąż ≥ próg wyzwalania** (czyli gdy maszyna stanów uznaje
kanał za wciąż aktywny, SIGNAL_DETECTED). Może zadziałać wyłącznie w trakcie
LINGER, gdy poziom już spadł poniżej progu wyzwalania, ale slot jest wciąż
otwarty na czas trzymania.

To jest świadome zabezpieczenie przed odtworzeniem błędu zaobserwowanego w
SDR++: niezależny (nie powiązany matematycznie z triggerem) drugi próg
squelcha potrafił tam wyciszać dźwięk **nawet gdy własna logika skanowania
programu uznawała sygnał za obecny** — dwa niepowiązane progi mogły się
rozjechać. Sam algorytm decydujący o SIGNAL_DETECTED/LINGER pozostaje
**całkowicie niezmieniony** przez squelch — squelch tylko odcina/wycisza to,
co byłoby i tak wypuszczone w trakcie LINGER, nigdy nie zmienia, kiedy LINGER
się kończy.

### 9.3 Co dokładnie squelch robi z blokiem poniżej progu

Dla każdego przetwarzanego bloku danego slotu, jeśli jego (skorygowany o
kompensację brzegową) poziom < efektywny próg squelcha:

- **Żywy głośnik**: ten blok jest całkowicie pomijany (cisza, nic nie jest
  wysyłane do wyjścia audio).
- **Nagranie** (jeśli nagrywanie włączone): zamiast pominąć blok, do bufora
  nagrania jest zapisywana **cisza o tej samej długości** — ale nie od razu:
  jest ona **wstrzymywana** (buforowana) zamiast natychmiast zapisana na dysk
  (patrz §9.4).
- Stan LINGER/czas trzymania samego slotu jest liczony **wyłącznie** względem
  progu wyzwalania, całkowicie niezależnie od tego, czy blok akurat jest
  squelchowany.

### 9.4 Cisza w środku nagrania kontra ucięcie ogona — dwie różne decyzje

To rozróżnienie było świadomym wynikiem opinii użytkownika i jest kluczowe:

- **Przerwa w środku trwającej rozmowy** (np. chwila ciszy między dwoma
  zdaniami tej samej transmisji, zanim poziom znów wzrośnie powyżej progu
  squelcha) — **musi zabrzmieć jak realna cisza**, nie zostać wycięta. Powód:
  jeśli wycięte fragmenty po prostu znikają z pliku, słuchacz nie ma jak
  odróżnić "komunikat był ciągły" od "to tylko kilkukrotnie wykryty sygnał z
  wyciętymi przerwami" — długość ciszy w nagraniu musi odpowiadać realnemu
  upływowi czasu.
- **Ogon na końcu transmisji** (squelchowany szum po ostatnim realnym dźwięku,
  aż do zamknięcia slotu) — **musi zostać obcięty**, nie zapisany jako cisza.
  Realna przerwa między dwoma osobnymi komunikatami ma sens **między
  nagraniami** (dwa osobne pliki po pełnym wygaśnięciu trzymania i ponownym
  wykryciu) — nie jako martwy ogon ciszy sklejony na końcu jednego pliku.

Ponieważ w momencie napotkania squelchowanego bloku **nie wiadomo z góry**,
który to przypadek (czy realny dźwięk jeszcze wróci, czy to już koniec
transmisji), algorytm bufora:

1. Squelchowany blok trafia do bufora "oczekującej ciszy" (nie jest od razu
   zapisywany do pliku).
2. Jeśli **po** buforze ciszy przyjdzie realny (nie-squelchowany) blok audio —
   **cała zbuforowana cisza jest najpierw zapisywana do pliku, w kolejności**,
   dopiero potem ten realny blok. To okazało się "przerwą w środku" — teraz
   ma poprawną długość w pliku.
3. Jeśli slot zostaje **zamknięty** (transmisja definitywnie się kończy), zanim
   jakikolwiek realny blok nadejdzie po buforze — cała zbuforowana cisza jest
   **odrzucona, nigdy niezapisana**. To był ogon — obcięty zgodnie z
   wymaganiem.

Bufor jest ograniczony (najwyżej tyle bloków, ile mieści się w jednym okresie
trzymania — LINGER i tak zamyka slot po pewnym czasie, więc bufor nie rośnie
w nieskończoność).

**Wznowienie po squelchowanej przerwie** musi zresetować stan redukcji szumów
(§10, jeśli włączona) tego slotu — redukcja szumów to procesor działający na
zachodzących na siebie oknach (STFT) z pamięcią między wywołaniami; jeśli nie
była karmiona podczas przerwy, jej pierwsze wywołanie po wznowieniu inaczej
sklei niedokończoną ramkę sprzed przerwy z zupełnie nowym, nieciągłym w czasie
audio, dając słyszalny trzask dokładnie w miejscu wznowienia. Demodulator
**nie** wymaga takiego resetu — działa co takt niezależnie od squelcha, więc
jego własny stan (faza, estymata DC) nigdy nie był przerywany.

### 9.5 "Fantomowe" nagrania

Jeśli cała transmisja od otwarcia do zamknięcia slotu składała się **wyłącznie**
z bloków squelchowanych (krótki skok powyżej progu wyzwalania, natychmiast
spadający poniżej progu squelcha, zanim slot zdążył zostać obsłużony choć raz
z realnym dźwiękiem) — powstały plik (w trybie split) musi zostać **usunięty**,
nigdy skatalogowany w bibliotece odtwarzacza jako realne nagranie. Bez tego
takie zdarzenie produkuje w pełni ciche pliki WAV zaśmiecające bibliotekę bez
niczego do usłyszenia.

---

## 10. Demodulacja

Wszystkie tryby modulacji operują na zespolonych próbkach IQ przechwyconego
okna, **przesuniętych cyfrowo do pasma podstawowego** (mieszanie z lokalnym
oscylatorem cyfrowym na przesunięciu danego kanału od środka okna) **przed**
decymacją i demodulacją — bo tuner w trybie szerokopasmowym stoi na środku
całego okna, nie na konkretnym kanale.

**Kolejność ma znaczenie i musi być zachowana**: mieszanie do pasma
podstawowego → decymacja (z filtrem antyaliasingowym) → dopiero demodulacja
(dla AM/NFM). Filtr antyaliasingowy decymacji jest tym, co faktycznie odrzuca
sąsiednie kanały obecne w szerokopasmowym przechwycie — demodulowanie
pełnoprędkościowego, wielokanałowego sygnału przed odfiltrowaniem pozwoliłoby
sąsiednim kanałom przeciekać do obwiedni AM / dyskryminatora FM, gdy w oknie
jest więcej niż jeden kanał naraz.

### 10.1 AM (domyślna dla lotnictwa cywilnego 118–136 MHz)

1. Zmieszaj do pasma podstawowego, zdecymuj do częstotliwości audio (np.
   12 500 Hz).
2. Weź obwiednię (moduł sygnału zespolonego).
3. Usuń składową stałą filtrem górnoprzepustowym pierwszego rzędu (jednobiegunowy
   filtr o współczynniku wygładzania α = 0.05: `dc[n] = (1−α)·dc[n−1] + α·v[n]`,
   `wyjście = v − dc`), z pamięcią stanu między wywołaniami (żeby transmisja
   zaczynająca się w środku bloku nie kliknęła).

### 10.2 NFM

1. Zmieszaj do pasma podstawowego, zdecymuj do częstotliwości audio.
2. Weź fazę sygnału zespolonego, rozwiń ją (unwrap) razem z ostatnią fazą z
   poprzedniego wywołania (ciągłość między blokami).
3. Wyjście audio to różnica kolejnych próbek fazy (dyskryminator FM), obcięta
   do zakresu [−3, 3] rad.

### 10.3 WFM (radio FM, opcjonalnie stereo)

WFM wymaga zupełnie innego traktowania: dewiacja ±75 kHz i pasmo zajmowane
~180–200 kHz (reguła Carsona) zostałyby przefiltrowane do szumu przez zwykłą
decymację do wąskiej częstotliwości audio *przed* demodulacją — poprawną
kolejnością dla wąskopasmowych AM/NFM.

1. Zdecymuj IQ do pośredniej częstotliwości (np. 200 kHz — wystarczająco
   szeroko, żeby zmieścić pełną dewiację ±75 kHz i pozostać powyżej 2× górnej
   krawędzi podnośnej stereo 53 kHz), ograniczonej z góry do faktycznej
   przepustowości przechwytu, jeśli jest węższa.
2. Weź fazę, rozwiń, dyskryminator FM (jak w NFM) — to sygnał kompozytowy
   (MPX).
3. **De-emfaza**: filtr dolnoprzepustowy pierwszego rzędu ze stałą czasową 50 μs
   (standard europejski/CCIR; USA używa 75 μs) — nadajniki z góry podbijają
   wysokie częstotliwości audio, licząc na to, że odbiornik je z powrotem
   ściągnie.
4. Jeśli **mono**: zdecymuj wynik do końcowej częstotliwości audio i zwróć.
5. Jeśli **stereo** (włączone globalnie, dotyczy tylko żywego odsłuchu —
   nagrania zawsze mono niezależnie od tego ustawienia):
   - Sygnał kompozytowy MPX niesie: mono L+R w 0–15 kHz, ton pilota 19 kHz,
     różnicę L−R zmodulowaną DSB-SC na (stłumionej) podnośnej 38 kHz — razem
     zajmując 23–53 kHz (ITU-R BS.450).
   - Wyodrębnij pasmowo (filtr pasmowoprzepustowy) ton pilota (19 kHz ± 2 kHz)
     i podnośną L−R (23–53 kHz), a dolnoprzepustowo (≤15 kHz) sygnał główny
     L+R.
   - Znormalizuj amplitudę pilota do jedności (śledzona wygładzona estymata
     RMS), następnie **podnieś do kwadratu** — podwaja to jego częstotliwość do
     dokładnie 38 Hz z dopasowaną fazą, co jest lekką alternatywą dla pełnej
     pętli synchronizacji fazy (PLL), potrzebnej do koherentnej demodulacji
     podnośnej.
   - Zdemoduluj koherentnie podnośną L−R tym odtworzonym odniesieniem 38 kHz,
     odfiltruj dolnoprzepustowo do pasma audio, pomnóż przez empiryczny
     współczynnik wzmocnienia (kompensujący utratę amplitudy przy podnoszeniu
     do kwadratu i to, że pilot niesie tylko ~10% szczytowej dewiacji
     kompozytu).
   - `L = (L+R) + (L−R)`, `R = (L+R) − (L−R)`, każdy przepuszczony osobno
     przez de-emfazę, zdecymowany do końcowej częstotliwości audio.

### 10.4 Normalizacja wyjścia (wszystkie tryby)

Po demodulacji: znajdź szczytową amplitudę bloku; jeśli >~1e-6, przeskaluj cały
blok tak, żeby szczyt osiągnął `0.8` (dzieląc przez `max(szczyt, 0.35)` —
dolne ograniczenie chroni bardzo ciche bloki przed nadmiernym wzmocnieniem
szumu). Wynik obcięty do [−1, 1].

---

## 11. Redukcja szumów (opcjonalna, adaptacyjna bramka widmowa)

Strumieniowy procesor spektralny do zdemodulowanego audio — dobrze dopasowany
do scenariusza słabej anteny, gdzie szum tła jest szerokopasmowy i obecny w
*każdej* ramce, a mowa jest przerywana.

### 11.1 Estymacja poziomu szumu (minimum-statistics)

Prosty "follower minimum klatka-po-klatce" nie działa tutaj — losowy szum
odbiornika ma na tyle dużą wariancję między kolejnymi klatkami, że przez czysty
przypadek spada poniżej "prawdziwego poziomu szumu" w około połowie klatek —
follower gonił by moc niemal wszędzie, zamiast osiąść na prawdziwym poziomie
podłogi. Zamiast tego:

1. Podziel audio na ramki STFT o rozmiarze np. 512 próbek, z zakładką 75%
   (skok = ¼ rozmiaru ramki) — nakładanie się tak duże jest tym, co utrzymuje
   płynność zmian zastosowanego wzmocnienia (bez trzasków/pompowania).
2. Dla każdego binu widmowego: krótkoterminowo wygładź moc wykładniczą średnią
   ruchomą (stała czasowa ~80 ms).
3. Estymata podłogi szumu tego binu = **minimum** tej wygładzonej mocy w
   trailing oknie ~1.5 sekundy (nie ostatnia wartość — minimum z historii).

### 11.2 Odejmowanie widmowe (spectral subtraction)

Dla każdego binu, ze współczynnikiem nadmiarowego odejmowania (np. 2.0):

```
wzmocnienie_bramki = 1 − 2.0 · √(moc_szumu / moc_biezaca)
```

obcięte z dołu do "podłogi widmowej" (np. 0.06, czyli −24 dB — zapobiega
"szumowi muzycznemu" powstającemu z pełnego wygaszania binów).

Efektywne zastosowane wzmocnienie = interpolacja między 1.0 (brak zmian) a
powyższym wzmocnieniem bramki, ważona parametrem "siła" (0–1) — pozwala to
płynnie regulować agresywność redukcji na żywo, bez trzasku.

Rekonstrukcja: odwrotne FFT z zastosowanym wzmocnieniem, technika
overlap-add znormalizowana przez zmierzoną (nie zakładaną teoretycznie) stałą
sumowania zachodzących na siebie okien.

### 11.3 Stan i reset

Stan (bufor wejściowy, bufor rekonstrukcji overlap-add, historia
minimum-statistics) jest **własnością pojedynczego slotu** i musi być
resetowany po wznowieniu z przerwy squelcha (§9.4).

---

## 12. Nagrywanie

### 12.1 Dwa tryby

- **Split** (domyślny): osobny plik na każdą transmisję (slot). Plik otwierany
  przy otwarciu slotu, zamykany przy jego zamknięciu.
- **Single**: jeden ciągły plik na całą sesję nasłuchu, otwierany przy
  starcie skanera. Między transmisjami w tym samym pliku (jeśli włączone)
  wstawiany jest krótki znacznik dźwiękowy: ton 1200 Hz przez 0.12 s, z
  narastaniem/opadaniem obwiedni (żeby nie kliknął), amplituda 0.25 —
  ułatwia lokalizację granic transmisji przy przewijaniu pojedynczego długiego
  pliku.

### 12.2 Format zapisu

WAV, mono, 16-bit PCM, częstotliwość próbkowania audio 12 500 Hz (ta sama, w
której pracuje demodulator).

### 12.3 Nazewnictwo plików (tryb split)

`<RRRR-MM-DD>/<RRRR-MM-DD_GG-MM-SS-mikrosekundy>_<kHz>.wav`, zapisywane od razu
w podfolderze daty (nie luzem, wymagającym późniejszego sortowania). Rozdzielczość
**mikrosekundowa** (nie tylko sekundowa) w znaczniku czasu jest konieczna: dwie
osobne transmisje na tej samej częstotliwości zaczynające się w tej samej
sekundzie zegarowej (realny, nierzadki przypadek przy wielokanałowym
skanowaniu — slot zamyka się i natychmiast otwiera ponownie na tej samej
częstotliwości bez zmiany okna) inaczej dostałyby identyczną nazwę pliku, a
druga cicho nadpisałaby pierwszą.

### 12.4 Metadane osadzone w pliku WAV

Po zamknięciu pliku (finalizacja nagłówka WAV) dopisywany jest standardowy
fragment RIFF `LIST`/`INFO`/`ICMT` zawierający blob JSON z pełnymi metadanymi
zebranymi przy otwarciu slotu (§7.3) plus dane zamknięcia: powód zamknięcia,
znacznik czasu UTC zamknięcia, rzeczywisty czas trwania. Odtwarzacze/narzędzia
nierozpoznające tego fragmentu po prostu go pomijają (standard RIFF wymaga
pomijania nierozpoznanych fragmentów) — ale np. `ffprobe`/`exiftool` go pokażą.
Rozmiar tego fragmentu musi być wyrównany do parzystej liczby bajtów (dopełniony
zerem w razie potrzeby — wymóg formatu RIFF), a główny nagłówek RIFF (pole
całkowitego rozmiaru pliku minus 8 bajtów) musi zostać poprawiony po dopisaniu.

### 12.5 Nagranie "fantomowe" — czyszczenie

Patrz §9.5 — plik bez ani jednego realnego (niezastąpionego ciszą) bloku audio
jest usuwany, nie katalogowany.

### 12.6 Nagrywanie a squelch

Patrz §9.3–9.4 pełna semantyka: cisza w środku, obcięcie ogona.

---

## 13. Odtwarzanie na żywo (wyjście audio)

### 13.1 Dopasowanie częstotliwości próbkowania

Demodulator pracuje na niskiej częstotliwości audio (np. 12 500 Hz) — żadna
karta dźwiękowa nie obsługuje tego natywnie. Strumień wyjściowy musi być
otwarty na **natywnej częstotliwości urządzenia wyjściowego** (typowo
44100/48000 Hz), z jawnym, wysokiej jakości przepróbkowaniem
(polifazowe, np. `resample_poly`) po stronie aplikacji — pozostawienie tego
sterownikowi/systemowi dźwięku dałoby typowo tańszą/gorszą konwersję.

### 13.2 Przepróbkowanie ciągłe bez trzasków na granicy bloków

Naiwne przepróbkowanie każdej ~30 ms działki osobno (bez kontekstu z sąsiednich
działek) niejawnie dopełnia zerami poza granicami wejścia — powodując mały
artefakt na *końcu* każdego bloku (filtr "widzi" ciszę zamiast prawdziwego
audio, które faktycznie po nim nastąpi). Rozwiązanie: zamiast przepróbkowywać
natychmiast, zatrzymuj mały margines *jeszcze nieprzepróbkowanych* próbek na
końcu każdego wywołania (razem z prawdziwą historią lewego kontekstu z
poprzedniego wywołania) — tak, żeby każda faktycznie wyemitowana próbka miała
prawdziwe audio jako kontekst filtra po **obu** stronach, nigdy sztuczne zera.
Kosztuje to stałe, niewielkie opóźnienie (kilka ms), nieistotne wobec własnych
opóźnień przestrajania/trzymania skanera.

### 13.3 Bufor pierścieniowy, oddzielenie od produkcji audio

Wywołanie zapisujące audio (z wątku skanera) **nigdy nie może blokować** —
wrzuca przepróbkowane audio do bufora pierścieniowego i wraca natychmiast.
Osobny mechanizm sterownika dźwięku (własny wątek/callback, na żądanie
urządzenia) pobiera z bufora, kiedy faktycznie potrzebuje kolejnej porcji do
odtworzenia — całkowicie odseparowany czasowo od tego, czym akurat zajęty jest
wątek skanera.

Ograniczenie górne bufora (np. 2 sekundy) zabezpiecza przed nieograniczonym
wzrostem pamięci, gdyby urządzenie wyjściowe kiedykolwiek się zawiesiło —
nadmiar jest odrzucany (najstarsze próbki), nigdy blokując zapisującego.

### 13.4 "Prime" (rozgrzewka bufora) i ponowne uzbrajanie po chwilowej przerwie

Zanim faktycznie zacznie się realne odtwarzanie (start sesji, oraz po
prawdziwym niedoborze danych), callback urządzenia **wstrzymuje** realne
odtwarzanie i cicho gra ciszę, dopóki bufor nie zbierze zapasu (np. 0.5 s) —
tak, żeby chwilowe zacięcie produkcji audio "zjadło" ten zapas zamiast być od
razu słyszalne jako przerwa.

**Pojedyncze** puste wywołanie callbacku (bufor akurat pusty) **nie** wymusza
ponownego pełnego "prime" — normalna różnica tempa produkcji (~30 ms takty
skanera) i konsumpcji (tempo wyznaczone przez sam sterownik audio) sprawia, że
bufor od czasu do czasu naturalnie "ociera się" o zero bez żadnego realnego
niedoboru. Dopiero **kilka kolejnych** pustych wywołań z rzędu (np. 4) uznawane
jest za prawdziwy, trwały niedobór, wymuszający ponowne "prime" — inaczej
każde takie normalne, nieszkodliwe "otarcie się o zero" samo w sobie wywoływało
słyszalną ~200 ms ciszę za każdym razem, co paradoksalnie *powodowało*
dokładnie ten przerywany dźwięk, któremu ten mechanizm miał zapobiegać.

### 13.5 Korekcja dryfu zegara (drift correction)

Zegar próbkowania SDR i zegar sprzętowy karty dźwiękowej to dwa niezależne
fizyczne oscylatory — nigdy nie są *dokładnie* identyczne, nawet oba będąc
nominalnie "poprawne". Bez korekcji, poziom bufora pierścieniowego powoli, ale
systematycznie **dryfuje** w jedną stronę (nie tylko szumi losowo) w trakcie
długiej sesji, aż w końcu wyczerpie zapas.

Rozwiązanie: prosty regulator proporcjonalno-całkujący (PI) porównujący
aktualny poziom bufora z celem (poziomem "prime") przy każdym zapisie i delikatnie
"naciągający"/"ściskający" liczbę próbek bieżącego bloku (przez interpolację
liniową) — rozciągając go, jeśli bufor jest za niski, ściskając, jeśli za
wysoki. Korekcja jest zawsze małym ułamkiem bieżącego błędu (żeby sama korekcja
była niesłyszalna, zbiegając stopniowo zamiast jednym skokiem) i nigdy więcej
niż stały ułamek długości bloku (np. 25%) niezależnie od wielkości błędu.
**Człon całkujący jest konieczny** — sam proporcjonalny regulator wobec
*trwałego* zaburzenia (stała różnica tempa zegarów) matematycznie osiada na
trwałym błędzie ustalonym zamiast kiedykolwiek faktycznie go zniwelować; człon
całkujący (dużo mniejszy, z ograniczeniem narastania) usuwa właśnie ten błąd
ustalony.

### 13.6 Wyjście stereo zawsze

Strumień wyjściowy jest zawsze otwierany jako **2-kanałowy**: mono
(AM/NFM/WFM bez stereo) jest po prostu duplikowane na oba kanały (L=R), a
prawdziwe stereo (WFM z włączonym dekodowaniem stereo) przechodzi bez zmian —
strumień nigdy nie musi być ponownie otwierany przy zmianie mono/stereo w
trakcie sesji.

---

## 14. Interfejs użytkownika

Pięć głównych zakładek: **Nasłuch**, **Kanały**, **Odtwarzacz**, **Statystyki**,
**Opcje**. Ciemny motyw kolorystyczny w całej aplikacji.

### 14.1 Zakładka Nasłuch (Skaner)

- Wybór urządzenia SDR (rozwijana lista: "Automatycznie", enumerowane
  urządzenia SoapySDR, "Symulacja"), niedostępny do zmiany podczas pracy skanera.
- Szerokość pasma przechwytu w MHz (podwójna rola — patrz §3.1).
- **Lista pasm/zakresów** (przewijalna): każdy wiersz to checkbox
  włącz/wyłącz, pole nazwy, checkbox "1 kanał" (pojedyncza częstotliwość
  zamiast zakresu — automatycznie ustawia stop=start), start/stop w MHz, krok w
  kHz, modulacja, waga priorytetu, przycisk "zaawansowane" (⚙, przełącza tryb
  edycji per-pasmo — patrz §6.6/§7.4), przycisk usunięcia.
- **Lista pamięci** jako rozwijany wiersz w tej samej liście (nie osobna
  zakładka) — checkbox włącz/wyłącz całą listę, licznik "N włączonych", i po
  rozwinięciu: drzewo włączonych kanałów pogrupowanych folderami, z
  edytowalną wagą każdego bezpośrednio w drzewie.
- Przyciski: "Dodaj preset" (z rozwijanej listy gotowych presetów pasm),
  "Dodaj własny zakres", "Lista pamięci (kanały)" (skrót do zakładki Kanały),
  szybkie dodanie pojedynczej częstotliwości.
- Suwaki (każdy: pasek + pole liczbowe, oba aktualizują się na żywo w trakcie
  przeciągania): Tuning Time, Linger Time, Trigger Level, Squelch (offset +
  włącznik), Gain (+ checkbox Auto/AGC), siła redukcji szumów (+ włącznik).
  Checkbox stereo WFM (dotyczy tylko żywego odsłuchu).
- W trybie edycji "zaawansowanej" jednego pasma: powyższe suwaki (poza
  squelchem, który zawsze jest globalny) zmieniają kolor na bursztynowy i
  kontrolują to jedno pasmo zamiast ustawień globalnych; przyciski "Zapisz"
  (zapisuje bieżące wartości jako nadpisania tego pasma) i "Przywróć domyślne"
  (czyści wszystkie nadpisania, wraca do globalnych).
- Miernik poziomu (pasek poziomy dBFS z linią progu wyzwalania).
- Podgląd widma na żywo (mini-wykres bieżącego okna przechwytu), z linią progu
  wyzwalania/squelcha (kolor bursztynowy, jeśli aktualnie tunowane pasmo ma
  własne nadpisanie progu).
- Etykieta "czas powrotu do tego okna" (rzeczywisty, zmierzony czas między
  dwoma kolejnymi wizytami tego samego okna — nie teoretyczne wyliczenie, bo
  ważone linie odwiedzane są w bardzo różnym rzeczywistym tempie).
- Nazwa/częstotliwość aktualnie aktywnego kanału (do 3 wierszy jednocześnie
  aktywnych kanałów, z dopiskiem "(+N więcej)" przy większej liczbie), stan
  tekstowy (IDLE/SCANNING/SIGNAL_DETECTED/LINGER/RECONNECTING).
- Przyciski: START/STOP, SKIP (pomiń bieżącą transmisję, częstotliwość
  pozostaje skanowalna), SKIP + BLACKLIST (jak wyżej, plus trwałe zablokowanie
  częstotliwości), WYCISZ (wyłącza tylko żywy odsłuch — nagrywanie działa
  dalej w tle, jeśli włączone).

### 14.2 Zakładka Kanały

Dwie podzakładki:

- **Kanały (Memory List)**: pole wyszukiwania (filtruje po częstotliwości,
  nazwie, modulacji, folderze — skrót Ctrl+F), drzewo kanałów pogrupowanych
  folderami (przeciąganie kanału do folderu zmienia jego przypisanie),
  checkbox włącz/wyłącz przy każdym kanale i przy folderze (checkbox folderu
  ma stan pośredni, jeśli tylko część dzieci jest włączona — kliknięcie
  ustawia wszystkie dzieci naraz). Przyciski: Dodaj, Edytuj (kanał lub — jeśli
  zaznaczony folder — zmiana jego nazwy), Usuń (też klawisz Delete), Nowy
  folder, "Wczytaj wojskowe (znane częstotliwości)", "Wczytaj straż pożarną
  (znane częstotliwości)" — oba dopisują gotową listę znanych publicznie
  częstotliwości (§14.3), nigdy nie nadpisując istniejącej listy (pomijają
  duplikaty po dokładnej częstotliwości).
- **Czarna lista (Blacklist)**: tabela (częstotliwość, margines w kHz, opis,
  data dodania), przyciski Dodaj ręcznie / Usuń (też klawisz Delete).

### 14.3 Gotowe presety (znane publicznie częstotliwości)

**Presety pasm** (do jednoklikowego dodania w zakładce Nasłuch):
lotnictwo cywilne (118–137 MHz, krok 8.33 kHz, AM), VHF wojskowe
(137–144 MHz, 25 kHz, AM), UHF wojskowe (225–400 MHz, 25 kHz, AM), PMR446
(446.00625–446.19375 MHz, 12.5 kHz, NFM), amatorskie 2 m (144–146 MHz,
12.5 kHz, NFM), amatorskie 70 cm (430–440 MHz, 12.5 kHz, NFM), VHF morskie
(156–162.025 MHz, 25 kHz, NFM), radio FM (87.5–108 MHz, 100 kHz, WFM), VHF
straży pożarnej (148.6625–149.3375 MHz, 12.5 kHz, NFM), PRM
(168.5–169.2 MHz, 12.5 kHz, NFM).

**Gotowe kanały wojskowe** (przycisk "Wczytaj wojskowe"): częstotliwości
guard, tankowanie w powietrzu (cysterny), sektory OAT Warszawa (EPWW), oraz
częstotliwości wież/podejść baz QRA/Baltic Air Policing (Malbork, Łask, Mińsk
Mazowiecki, Powidz, Świdwin, Mirosławiec).

**Gotowe kanały straży pożarnej** (przycisk "Wczytaj straż pożarną"): ogólnopolskie
kanały ratowniczo-gaśnicze PSP (KRG 1–8), oraz częstotliwości alarmowania
syren OSP (Tx/Rx w paśmie 148.6625–149.3375 MHz) — wiele OSP wciąż pracuje
analogowo mimo migracji PSP do cyfrowego DMR.

### 14.4 Zakładka Odtwarzacz

Główny widok: pasek narzędzi u góry, panel boczny z listą częstotliwości/
języków, tabela nagrań (środek/prawo), panel transkrypcji, pasek odtwarzania
na dole.

**Pasek narzędzi**: przycisk Odśwież (F5) — segreguje nowe pliki i ponownie
skanuje bibliotekę; pasek postępu; checkbox "Czas UTC" (przełącza wyświetlanie
czasu w całej zakładce); filtr daty (Wszystkie / Dzisiaj / Ostatnie 24h /
Ostatnie 7 dni / Ostatnie 30 dni / konkretny dzień z listy); suwak minimalnej
jakości (0–100); checkbox "Transkrypcja auto" z etykietą postępu; pole
wyszukiwania pełnotekstowego (Ctrl+F — przeszukuje nazwę pliku, częstotliwość,
datę, treść transkrypcji); przyciski szybkiego sortowania "Najnowsze"/
"Najlepsza jakość".

**Panel boczny częstotliwości/języków**: przełącznik trybu Częstotliwości/
Języki, kombo sortowania (3 opcje w każdym trybie), wpis "Wszystkie" i
"★ Ulubione" na górze, poniżej lista pogrupowana odpowiednio po częstotliwości
(z nazwą, jeśli znana z Listy pamięci) lub po wykrytym/wymuszonym języku
transkrypcji (z etykietami pełnych nazw dla popularnych języków, plus
"(bez transkrypcji)" dla nagrań bez wyniku). Wybór w jednym trybie czyści
filtr z drugiego (nigdy nieoczekiwanie się nie łączą).

**Tabela nagrań** — kolumny: Data, Godzina, Częstotliwość (z nazwą, jeśli
znana), Czas trwania, SNR (dB), Jakość (0–100, tło komórki kolorowane
czerwony→żółty→zielony wg wyniku; szare tło = plik nieczytelny), Rozmiar,
Transkrypcja (snippet tekstu albo etykieta statusu: "⏳ w kolejce", "– za
krótkie", "🔇 brak mowy", "⚠ błąd transkrypcji", "❓ niska pewność"; tło
kolorowane wg statusu). Sortowalna po dowolnej kolumnie (numerycznie/czasowo,
nie leksykograficznie). Zaznaczenie wiersza (klik lub strzałki) odtwarza
nagranie. Menu kontekstowe (PPM): Odtwórz, Pokaż w folderze (z zaznaczonym
plikiem, przez usługę D-Bus menedżera plików, z zapasowym zwykłym otwarciem
folderu), Kopiuj ścieżkę, Dodaj/usuń z ulubionych, Zapisz częstotliwość,
Transkrybuj ponownie, FlightRadar24, ADSBExchange, Usuń — z wariantem dla
wielokrotnego zaznaczenia (operacje zbiorcze). Klawisz Delete usuwa zaznaczone
(z potwierdzeniem, wypisując nazwy plików).

**Panel transkrypcji**: nagłówek z częstotliwością i statusem, przyciski
FlightRadar24/ADSBExchange (otwierają playback w przeglądarce o czasie tego
nagrania — patrz §17.5), "Zapisz częstotliwość" (dialog: nazwa + folder,
zapisuje/nadpisuje w Liście pamięci), przycisk ulubionych (☆/★), wybór języka
do wymuszenia przy ręcznej transkrypcji ("Auto (wykryj)" / Polski / English /
dowolny wpisany kod ISO), przycisk "Transkrybuj ponownie" (pomija cache).
Pole tekstowe transkrypcji jest **edytowalne ręcznie** — zmiana zapisuje się
automatycznie po przejściu do innego nagrania (puste pole = cofnięcie do
"brak transkrypcji"; wypełnione = zapisane jako wynik ze statusem "ok" i
silnikiem "manual", widoczne w tabeli identycznie jak wynik automatyczny).

**Pasek odtwarzania**: widok fali dźwiękowej pełnej szerokości (zamiast
zwykłego paska postępu — patrz §14.5), pod nim: przyciski ⏮/▶⏸/⏹/⏭, etykieta
"teraz odtwarzane", czas (mm:ss / mm:ss), suwak głośności, checkbox
"Auto-odtwarzanie kolejnych" (domyślnie włączony — po zakończeniu nagrania
automatycznie odtwarza kolejne z listy, do szybkiego przeglądu ruchu).
Spacja = play/pause (globalny skrót w tej zakładce).

### 14.5 Widok fali dźwiękowej (Odtwarzacz)

Zastępuje zwykły pasek postępu — pełni tę samą rolę (klik lub przeciągnięcie =
przewinięcie do tej pozycji), jednocześnie pokazując amplitudę w czasie:

- Amplituda szczytowa (0–1) w N kubełkach (np. 400) pokrywających cały plik —
  obliczana raz przy starcie odtwarzania danego pliku (szybkie, wektoryzowane
  odczytanie i redukcja całego pliku, wykonywane synchronicznie, bez wątku w
  tle, bo pojedynczy plik nagrania to zwykle sekundy-dziesiątki sekund).
- Rysowanie: szare słupki dla części jeszcze nieodtworzonej, zielone dla już
  odtworzonej, pionowa linia głowicy odtwarzania, cienka pozioma linia
  środkowa. Dwie wersje (szara/zielona) są renderowane raz do bitmapy i tylko
  przycinane/nakładane przy każdej aktualizacji pozycji (kilka razy na
  sekundę) — nie rysowane od zera przy każdej klatce.
- Brak danych (plik nieczytelny lub jeszcze nie policzone) → płaska linia
  zamiast słupków, żeby odróżnić "brak danych" od "faktycznie cicho przez cały
  plik" (to drugie wygląda jak bardzo niskie słupki, nie płaska linia — obie
  sytuacje muszą być wizualnie rozróżnialne).
- Pozwala od razu zobaczyć np. przerwę wyciszoną przez squelch (§9) jako
  wyraźnie cichszy odcinek, bez konieczności odtwarzania na ślepo.

---

## 15. Zakładka Statystyki

Górny panel podsumowania: liczba nagrań, łączny czas trwania, zakres dat, średnia
jakość, najaktywniejsza częstotliwość, liczba unikalnych częstotliwości.

Tabela częstotliwości: częstotliwość, liczba nagrań, średnia jakość, łączny
czas trwania, ostatnio słyszana — sortowalna, podwójny klik przenosi do
zakładki Odtwarzacz przefiltrowanej do tej częstotliwości.

Widok przełączany między podsumowaniem a szczegółem, z wykresami słupkowymi:
liczba nagrań per dzień, rozkład godzinowy (0–23), rozkład wg dnia tygodnia
(Pon–Nd, zawsze wszystkie 7 dni pokazane nawet z zerem).

Wszystkie statystyki respektują przełącznik czasu lokalnego/UTC.

---

## 16. Zakładka Opcje

- **Nagrywanie**: włącz/wyłącz, tryb Single/Split, katalog docelowy (wybór
  folderu), wstawianie znacznika dźwiękowego (tylko tryb Single).
- **Recency boost**: włącz/wyłącz, czas trwania boostu (5–600 s, krok 5),
  odstęp między dodatkowymi wizytami (1–120 s, krok 1).
- **Transkrypcja**: włącz/wyłącz automatyczną kolejkę w tle (ten sam
  przełącznik co w pasku narzędzi Odtwarzacza — oba zsynchronizowane).

Wszystkie zmiany w tej zakładce zapisują się natychmiast do trwałej
konfiguracji i (tam gdzie dotyczy trwającego skanu) działają od razu, bez
restartu.

---

## 17. Biblioteka nagrań (Odtwarzacz — logika danych)

### 17.1 Segregacja plików

Luźne pliki `.wav` leżące bezpośrednio w katalogu nagrań (nie w podfolderze
daty — np. z wcześniejszej wersji programu) są automatycznie przenoszone do
`<katalog>/<RRRR-MM-DD>/` na podstawie daty zakodowanej w nazwie pliku.
Operacja jest **idempotentna** (bezpieczna do wielokrotnego uruchamiania) —
pliki już w podfolderach nie są ruszane. Kolizja nazw (nie powinna się zdarzyć
dzięki mikrosekundowej precyzji, ale na wszelki wypadek) dopisuje sufiks
zamiast nadpisać/zgubić dane.

### 17.2 Parsowanie nazwy pliku

Wyrażenie dopasowujące format `RRRR-MM-DD_GG-MM-SS[-mikrosekundy]_kHz.wav`
(mikrosekundy opcjonalne — starsze nazwy sprzed tej zmiany parsują się dalej,
z zerowymi mikrosekundami). Data/godzina interpretowana jest jako czas polski.

### 17.3 Ocena jakości nagrania

Dla każdego pliku (z pamięci podręcznej kluczowanej ścieżką+rozmiarem+czasem
modyfikacji — patrz §17.6 — albo przeliczana od nowa, jeśli plik się zmienił):

1. Wczytaj próbki, policz obwiednię RMS w oknach ~50 ms.
2. **Odfiltruj okna o dokładnie zerowej amplitudzie** z dalszych obliczeń —
   to są okna zastąpione ciszą przez squelch (§9.3), nie realny szum
   odbiornika (prawdziwy szum termiczny/kwantyzacji praktycznie nigdy nie
   trafia całym ~50 ms oknem w dokładne zero). Bez tego filtra, nagranie z
   wystarczającą liczbą squelchowanych przerw (częsty przypadek — każda
   transmisja z pauzą w środku) pcha 15. percentyl "podłogi szumu" do
   dokładnego zera, co przy dzieleniu daje bezsensowne SNR rzędu 100+ dB i
   jakość przypiętą na 100 — dla nagrania, które często jest w rzeczywistości
   ciche/marginalne. Jeśli *wszystkie* okna są zerowe (przypadek skrajny —
   praktycznie niemożliwy dla realnego pliku, który przeszedł już przez
   czyszczenie nagrań fantomowych), użyj oryginalnych, niefiltrowanych
   tablic zamiast dzielić przez pustą selekcję.
3. Poziom tła (szumu) = 15. percentyl pozostałej (realnej) obwiedni RMS.
4. Poziom sygnału = 92. percentyl.
5. SNR w dB = `20·log₁₀(poziom_sygnału / poziom_tła)` (oba z minimalnym
   podłogowaniem 1e-6, żeby uniknąć dzielenia przez 0).
6. Współczynnik przesterowania = ułamek próbek (z realnych, niefiltrowanych
   danych) o wartości bezwzględnej ≥ 97.5% pełnej skali.
7. Wynik jakości (0–100):
   - Składowa SNR: `SNR` obcięte do [0, 30] dB, przeskalowane liniowo do
     [0, 100] (30 dB = wynik 100).
   - Kara za przesterowanie: `min(współczynnik_przesterowania · 300, 40)`
     punktów.
   - Kara za krótkie nagranie: 15 punktów, jeśli czas trwania < 1.0 s.
   - Wynik końcowy = `max(0, min(100, składowa_SNR − kara_przesterowania −
     kara_krótkie))`.
8. Plik nieczytelny/uszkodzony → metryki zerowe, flaga `readable=False`
   (wyświetlane jako "uszkodzony", nie jako "jakość 0" — semantycznie różne
   rzeczy).

### 17.4 Obliczanie fali dźwiękowej

Patrz §14.5 — maksimum wartości bezwzględnej próbek w N równych kubełkach
pokrywających cały plik, znormalizowane do [0, 1].

### 17.5 Linki zewnętrzne (FlightRadar24 / ADSBExchange)

Dla wybranego nagrania, na podstawie jego czasu UTC oraz skonfigurowanej
lokalizacji odbiornika (lat/lon):

- FlightRadar24: `https://www.flightradar24.com/{data}/{godzina:minuta}/20x/{lat},{lon}/9`
- ADSBExchange: `https://globe.adsbexchange.com/?replay={data}-{godzina:minuta}&lat={lat}&lon={lon}&zoom=8.8`

Otwierane w domyślnej przeglądarce systemowej.

### 17.6 Pamięci podręczne

- **Jakość** (`QualityCache`): klucz = `ścieżka|rozmiar_bajtów|czas_modyfikacji_sekundy`.
  Wpisy dla plików, które już nie istnieją, są usuwane przy pełnym odświeżeniu
  (`prune`). Zapis debounced (opóźniony, np. o 5 s po ostatniej zmianie) przy
  pojedynczych nowych nagraniach napływających w trakcie trwającego skanu —
  natychmiastowy zapis całego (potencjalnie dużego) pliku cache po **każdej**
  zakończonej transmisji był zmierzonym źródłem zacięć GUI, rosnącym wraz z
  rozmiarem zgromadzonego archiwum.
- **Transkrypcja** (`TranscriptionCache`): analogicznie, tym samym kluczem.
- **Ulubione** (`FavoritesStore`): zbiór tych samych kluczy — sama gwiazdka to
  fakt binarny, nie ma nic do przeliczania.

Pamięć podręczna **nigdy nie wykrywa sama z siebie**, że sam algorytm oceny
się zmienił (tylko że plik się zmienił) — jeśli algorytm oceny jakości ulegnie
zmianie, istniejące wpisy pamięci podręcznej dla niezmienionych plików trzeba
jawnie unieważnić (usunąć), inaczej pokazują wynik policzony starym
algorytmem w nieskończoność.

### 17.7 Dodawanie nowo zakończonego nagrania "na gorąco"

Zamiast czekać na pełne okresowe przeskanowanie katalogu, każda właśnie
zakończona transmisja (sygnał z silnika skanera) jest doklejana do biblioteki
natychmiast: sparsowanie nazwy, odczyt/policzenie jakości, dociągnięcie
transkrypcji/statusu ulubionego z pamięci podręcznych, wstawienie do modelu
tabeli **bez resetowania całej tabeli** (żeby nie gubić zaznaczenia/przewinięcia
widoku). Odświeżenie panelu bocznego częstotliwości i zakładki Statystyki po
takim zdarzeniu jest **debounced** (np. raz na sekundę, z ograniczeniem, by
ciągły napływ nie odkładał tego w nieskończoność) — pełne przebudowanie
wykresów/tabeli częstotliwości dla dużego archiwum jest zbyt kosztowne, by
robić to przy każdej pojedynczej transmisji.

---

## 18. Transkrypcja (Whisper, dwumodelowa)

### 18.1 Wybór modelu wg wykrytego języka

Dwa modele wczytywane leniwie (przy pierwszym użyciu), dzielone między
kolejnymi wywołaniami:

- **Model ogólny, wielojęzyczny** (duży, np. "large-v3") — używany do
  wykrycia języka i do transkrypcji wszystkiego poza angielskim.
- **Model angielski dostrojony do frazeologii ATC** — używany wyłącznie, gdy
  rozpoznanym/wymuszonym językiem jest angielski.

### 18.2 Rozstrzyganie wykrytego języka

Detekcja języka Whisper na krótkich, zaszumionych klipach VHF bywa zawodna —
na samym szumie potrafi z niską pewnością wskazać przypadkowy egzotyczny
język (obserwowane: norweski, fiński, rosyjski) zamiast prawdziwej treści
angielskiej/polskiej. Algorytm:

1. Jeśli zwycięski język detekcji jest jednym z "kandydujących" (domyślnie
   `en`, `pl`) — użyj go wprost.
2. W przeciwnym razie: znajdź kandydata z najwyższym prawdopodobieństwem
   spośród listy kandydujących języków. Jeśli jego prawdopodobieństwo wynosi
   co najmniej 60% prawdopodobieństwa zwycięzcy — użyj tego kandydata zamiast
   zwycięzcy.
3. Inaczej zostaw oryginalny wynik detekcji.

Ręczne "Transkrybuj ponownie" z wymuszonym językiem w GUI całkowicie pomija tę
detekcję (prawdopodobieństwo ustawione na 1.0) — nigdy nie wpływa na
zachowanie automatycznej kolejki w tle.

### 18.3 Transkrypcja

Pełny beam search (nie zachłanny/strumieniowy) z rozmiarem wiązki 5, z
filtrem VAD (detekcja aktywności głosowej), bez warunkowania na poprzednim
tekście (`condition_on_previous_text=False` — każdy plik to osobna,
niepowiązana transmisja). Priorytet: jakość, nie szybkość — to nie jest
transkrypcja "na żywo".

**Świadomie brak `initial_prompt`** z przykładową frazeologią lotniczą —
próba naprowadzenia słownictwa okazała się niebezpieczna: przy
niepewnym/zaszumionym audio model potrafił dosłownie przepisać fragmenty
promptu jako "transkrypcję" zamiast prawdziwej treści, a nawet krótsza wersja
(sama lista słów kluczowych) czasem pogarszała jakość dobrych nagrań.
Fabrykowanie wiarygodnie brzmiącego, ale zmyślonego tekstu jest gorsze niż
brak transkrypcji.

### 18.4 Klasyfikacja statusu wyniku

Progi dobrane empirycznie na realnych nagraniach z tego projektu:

- `no_speech` — brak segmentów mowy, albo czas trwania po VAD < 0.3 s.
- `low_confidence` — jeśli którykolwiek z warunków: prawdopodobieństwo języka
  < 0.4, średnie `avg_logprob` segmentów < −0.9, średnie `no_speech_prob` >
  0.5, maksymalny `compression_ratio` > 2.4 (typowy sygnał halucynacji/
  powtórzeń modelu).
- `ok` — w przeciwnym razie.
- `skipped_short` — nagranie krótsze niż skonfigurowany próg minimalny (0.6 s)
  — pomijane jeszcze przed uruchomieniem modelu.
- `error` — wyjątek w trakcie przetwarzania (uszkodzony plik, brak
  pamięci/modelu...) — nigdy nie propagowany na zewnątrz, żeby jeden zły plik
  nie zatrzymał całej kolejki w tle. **Traktowany jako nietrwały** — inaczej
  niż pozostałe statusy, automatyczna kolejka **sama z siebie** próbuje
  ponownie przy następnej okazji (bez klikania "Transkrybuj ponownie" za
  każdym razem), ale z ograniczeniem częstotliwości ponawiania (patrz §18.6).

### 18.5 Wybór urządzenia (GPU/CPU) i odporność na awarie

- `"auto"` (domyślne) próbuje najpierw GPU. Punkt startowy dla trybu
  obliczeniowego `"auto"` na GPU to **od razu** wariant oszczędny pamięciowo
  (wagi jako int8, obliczenia w float16) — nie pełny float16 — bo oba modele w
  pełnym float16 razem to obserwowany realnie ciasny margines na kartach z
  ograniczoną pamięcią (typowo 8 GB), zawodzący prędzej czy później i tak
  lądujący w tym samym wariancie oszczędnym, tylko po stracie czasu na próbę i
  błąd.
- Jeśli załadowanie/inferencja na GPU zawiedzie z błędem związanym z CUDA:
  najpierw spróbuj ponownie na GPU w wariancie jeszcze oszczędniejszym
  pamięciowo (jeśli jeszcze tego nie próbowano); dopiero **druga** awaria GPU
  (albo awaria wariantu już wymuszonego) przełącza na CPU.
- Przejście na CPU **nie jest trwałe do końca sesji** — po skonfigurowanym
  czasie oczekiwania (np. 30 minut) kolejna próba transkrypcji ponownie
  próbuje GPU od zera. Uzasadnienie: obserwowane realnie, że "trwałe do końca
  sesji" w praktyce oznaczało "prawie każda sesja" (7 z 7 ostatnich sesji w
  jednym z dzienników zakończyło się na CPU, zawsze z powodu chwilowego braku
  pamięci GPU) — realnie zwykle jest to chwilowy skok/fragmentacja pamięci
  GPU (np. po jednym wyjątkowo długim nagraniu), a nie trwały brak zasobu, i
  zwykle miałoby szansę wrócić do działania, gdyby dać mu tę szansę. Ten
  mechanizm ponawiania jest **samoograniczający się**: nieudana próba po
  prostu wraca na CPU z odświeżonym licznikiem czasu, więc nigdy nie zamienia
  się w próbę co plik.
- Transkrypcja na CPU **musi zostawić rezerwę rdzeni procesora** (np. 4)
  nietkniętą — to właśnie te rdzenie są potrzebne w czasie rzeczywistym
  wątkowi skanera/audio. Obserwowane realnie: w pełni zajęty procesor podnosi
  realny poziom szumu w nasłuchu (opóźnione odczyty ze sprzętu → sklejane
  nieciągłe próbki → szerokopasmowy artefakt w FFT), co przy niskim progu
  wyzwalania potrafi zalać wykrywanie fałszywkami — a każda z nich trafia do
  tej samej kolejki transkrypcji, jeszcze bardziej obciążając CPU (błędne
  koło, któremu ta rezerwa zapobiega).

### 18.6 Kolejka w tle — dwa tryby budowania listy "do zrobienia"

- **Pełne nadrobienie zaległości** (start programu, ręczne odświeżenie, albo
  wznowienie po zakończonej paczce): skanuje **całe** bieżące archiwum
  przeciwko pamięci podręcznej — każdy plik bez wyniku, oraz (tylko w tym
  trybie) każdy plik ze statusem `error`, trafia do kolejki.
- **Nowo zakończone pojedyncze nagranie**: dodawane do kolejki od razu, bez
  ponownego skanowania całego archiwum (uniknięcie kosztu rosnącego z
  rozmiarem archiwum przy każdej pojedynczej transmisji).
- **Wznowienie zaraz po zakończonej paczce**: sprawdza tylko naprawdę nowe
  pliki (bez żadnego wyniku), **celowo pomijając** ponowną próbę plików ze
  statusem `error` z tej samej paczki — bez tego wyjątku, plik kończący się
  błędem natychmiast kwalifikowałby się do kolejnej paczki, która zakończyłaby
  się tym samym błędem, w kółko, bez żadnej przerwy (obserwowany realnie
  przypadek nieskończonej pętli obciążającej GPU/CPU).
- Zapis pamięci podręcznej transkrypcji w trakcie przetwarzania paczki jest
  throttlowany (np. co najwyżej raz na 5 sekund, plus zawsze raz na koniec) —
  zapis **całego** pliku pamięci podręcznej po każdym pojedynczym
  przetworzonym nagraniu skalowałby koszt z rozmiarem całego archiwum, nie
  tylko nowej paczki.

---

## 19. Kwantyzacja czasu skanu — wyjaśnienie skokowego zachowania "czasu powrotu"

Dla zrozumienia obserwowanego zachowania (nie zmiana funkcjonalna): przy
niskiej wartości Tuning Time, rzeczywisty czas jednego przebiegu skanowania
zmienia się **skokowo**, nie płynnie, wraz z drobnymi zmianami tego ustawienia
— bo rzeczywisty czas realizacji jednego takcie (odczyt jednej "działki"
próbek) jest w praktyce skwantowany przez rzeczywisty czas trwania
sprzętowego/sterownikowego odczytu próbek (rzędu dziesiątek ms), nie przez
samą wartość ustawionego czasu przestrajania — bardzo krótki Tuning Time (np.
poniżej czasu trwania jednej działki) i tak efektywnie "zaokrągla się w górę"
do najbliższej wielokrotności rzeczywistego czasu odczytu sprzętu. Nie jest to
błąd zaokrąglenia w logice programu — to fizyczne ograniczenie tego, jak
drobno w czasie można w ogóle podzielić strumień próbek z danego urządzenia
przy danej częstotliwości próbkowania. Sensowna dolna granica Tuning Time to
taka, przy której tuner realnie zdążył się ustabilizować (typowe SDR-y
tanie/RTL-SDR: rzędu kilkudziesięciu ms) — zbyt niska wartość nie przyspiesza
już nic ponad ten naturalny krok kwantyzacji, tylko ryzykuje nasłuch części
jeszcze niestabilnego sygnału tunera.

---

## 20. Odporność operacyjna (diagnostyka, nie funkcja użytkowa)

Poniższe nie zmienia zachowania widocznego dla użytkownika, ale jest częścią
tego, jak program pozostaje **diagnozowalny** w terenie — warto odwzorować,
jeśli program ma być równie łatwy do utrzymania:

- Instalacja globalnego handlera sygnałów niskiego poziomu (SIGSEGV/SIGABRT/
  SIGBUS/SIGFPE), zrzucającego ślad stosu Pythona do pliku (nie na standardowe
  wyjście) w razie twardego crasha natywnego (np. wewnątrz sterownika C SDR),
  którego żaden mechanizm wyjątków wyższego poziomu nie jest w stanie
  przechwycić.
- Plik "checkpointów" startowych — bardzo prosty, zawsze aktywny dziennik
  tekstowy nadpisywany na nowo przy każdym uruchomieniu, zapisujący kolejne
  nazwane etapy startu silnika (otwarcie backendu, uruchomienie wątków
  pomocniczych, pierwsze przestrojenie, pierwszy odczyt...) — pozwala ustalić,
  do którego dokładnie etapu proces doszedł, zanim ewentualnie zniknął bez
  śladu.
- Ciągłe, zawsze aktywne logowanie diagnostyczne odczytu sprzętu (bez
  potrzeby włączania trybu debug) do osobnego pliku: ile odczytów wymagało
  więcej niż jednego wywołania sprzętowego, ile wystąpiło błędów przepełnienia
  bufora sprzętu, mediana/maksimum czasu odczytu, obciążenie systemu — z
  rozmiarem ograniczonym górnym limitem (cichy stop logowania po przekroczeniu,
  nie zapełnianie dysku).
- Opcjonalny (włączany zmienną środowiskową), bardziej szczegółowy tryb
  debugowania timingu na żywo: co sekundę raportuje, ile taktów przetwarzania
  przekroczyło budżet czasu rzeczywistego, ich maksimum, poziom zapełnienia
  bufora audio na żywo — oraz osobno to samo dla czasu samego blokującego
  odczytu sprzętu (rozróżnienie: "za dużo pracy na takt" wymaga innej naprawy
  niż "sprzęt/USB nie dostarcza danych w czasie rzeczywistym").
