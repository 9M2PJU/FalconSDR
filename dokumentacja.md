# FalconSDR — dokumentacja funkcjonalna

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
3. **Interfejs użytkownika w przeglądarce** — pięć stron (Nasłuch, Nagrania,
   Kanały, Statystyki, Ustawienia) serwowanych przez sam program po HTTP;
   dowolna liczba przeglądarek (komputer, telefon) może być podłączona
   jednocześnie. Trwała konfiguracja jest zapisywana na dysku po stronie
   programu, nie przeglądarki (§21).

Silnik skanera działa **niezależnie od tego, czy jakakolwiek przeglądarka jest
podłączona i która strona jest widoczna** — zamknięcie karty nie zatrzymuje
ani nie spowalnia nasłuchu.

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
| Katalog docelowy | `~/FalconSDR/recordings` |
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

Usunięte razem z odsłuchem przez lokalny głośnik — dźwięk (na żywo i w
nagraniach) jest zawsze mono.

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

`home_lat`/`home_lon` (domyślnie nieustawione — `None`) — używane do budowania
linków playbacku FlightRadar24/ADSBExchange (§17.5).

### 3.12 Inne ustawienia globalne

- Domyślny margines wpisu na czarnej liście: 5000 Hz.
- Szerokość okna mocy per-kanał w trybie Listy pamięci: 12 500 Hz (w trybie
  pasm zamiast tego używany jest krok danego pasma).
- Znane nazwy folderów Listy pamięci (żeby puste foldery nie znikały z drzewa).

### 3.13 Pliki na dysku

- Konfiguracja: `config.json` w katalogu konfiguracyjnym (np.
  `~/.config/FalconSDR/`).
- `channels.csv`, `blacklist.csv` w tym samym katalogu.
- Nagrania: `<output_dir>/<RRRR-MM-DD>/<znacznik_czasu>_<kHz>.wav` w trybie
  split (patrz §11.3); pojedynczy plik sesji w trybie single.
- Pamięć podręczna jakości nagrań, pamięć podręczna transkrypcji, lista
  ulubionych, lista nagrań skopiowanych na inne komputery (`mirrored.json`)
  — osobne pliki JSON w katalogu danych aplikacji, zapisywane atomowo (zapis
  do pliku tymczasowego i podmiana).
- Na komputerze pomocniczym (§21): `worker_state.json` w katalogu
  konfiguracyjnym (czy jednorazowy import metadanych już się odbył).
- Dziennik kalibracji mocy (`power_calibration.jsonl`) i pochodny z niego cache
  krzywej kompensacji brzegowej (§8) — w katalogu konfiguracyjnym.

---

## 4. Model danych

### 4.1 Kanał (Channel)

`freq_hz, name, modulation (AM/NFM/WFM), enabled, folder, weight`.

### 4.2 Wpis czarnej listy (BlacklistEntry)

`freq_hz, margin_hz (domyślnie 5000), label, date_added`. Częstotliwość jest
zablokowana, jeśli `|f − freq_hz| ≤ margin_hz`.

### 4.3 Stan skanera (do wyświetlenia w interfejsie)

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

### 5.4 Podgląd widma (interfejs)

Osobna, tańsza redukcja tego samego FFT do ustalonej liczby punktów (np. 512)
do wykresu w interfejsie: każdy punkt wyświetlany to **suma** (nie średnia!) mocy PSD
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

### 10.3 WFM (radio FM)

WFM wymaga zupełnie innego traktowania: dewiacja ±75 kHz i pasmo zajmowane
~180–200 kHz (reguła Carsona) zostałyby przefiltrowane do szumu przez zwykłą
decymację do wąskiej częstotliwości audio *przed* demodulacją — poprawną
kolejnością dla wąskopasmowych AM/NFM.

1. Zdecymuj IQ do pośredniej częstotliwości (np. 200 kHz — wystarczająco
   szeroko, żeby zmieścić pełną dewiację ±75 kHz), ograniczonej z góry do faktycznej
   przepustowości przechwytu, jeśli jest węższa.
2. Weź fazę, rozwiń, dyskryminator FM (jak w NFM) — to sygnał kompozytowy
   (MPX).
3. **De-emfaza**: filtr dolnoprzepustowy pierwszego rzędu ze stałą czasową 50 μs
   (standard europejski/CCIR; USA używa 75 μs) — nadajniki z góry podbijają
   wysokie częstotliwości audio, licząc na to, że odbiornik je z powrotem
   ściągnie.
4. Zdecymuj wynik do końcowej częstotliwości audio.

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

## 13. Odsłuch na żywo (w przeglądarce)

- Dźwięk na żywo to zawsze **aktualnie główny slot** (§7.3) — ten sam, mono,
  już po redukcji szumów i blokadzie szumów, który trafia do nagrania.
- Program wysyła go jako surowy strumień PCM 16-bit mono w natywnej
  częstotliwości demodulatora (np. 12 500 Hz), jednym długim żądaniem HTTP
  (nagłówek z częstotliwością próbkowania). Każdy słuchacz ma własną,
  ograniczoną kolejkę (np. 50 bloków ~30 ms): wolny lub zawieszony słuchacz
  traci najstarsze bloki, nigdy nie spowalnia silnika.
- Gdy nic nie gra (brak transmisji), co ~5 s idzie 10 ms ciszy — utrzymuje
  połączenie i pozwala wykryć słuchacza, który zniknął.
- Przeglądarka planuje każdy odebrany blok jako osobny bufor dźwiękowy, jeden
  za drugim, z ~0,25 s zapasu; jeśli kolejka wybiegła za bardzo w przyszłość
  (>1,2 s) albo została w tyle, zaczyna od nowa z tym samym zapasem —
  przerwy między transmisjami same "resetują" opóźnienie. Przepróbkowanie do
  częstotliwości karty dźwiękowej robi przeglądarka. Zerwane połączenie jest
  wznawiane po ~1 s.
- Celowo **bez** wątków audio wymagających bezpiecznego kontekstu (HTTPS):
  interfejs jest zwykle otwierany przez `http://<IP>` w sieci domowej.
- Wyciszenie/głośność są **per przeglądarka** — nie wpływają na nagrywanie
  ani na innych słuchaczy.

---

## 14. Interfejs użytkownika (przeglądarka)

Pięć stron: **Nasłuch**, **Nagrania**, **Kanały**, **Statystyki**,
**Ustawienia**; na telefonie nawigacja jest na dole ekranu. Motyw jasny,
ciemny lub zgodny z systemem (zapamiętywany w przeglądarce). W nagłówku
zawsze: stan skanera, wskaźnik nagrywania i przycisk odsłuchu na żywo.
Zmiany ustawień działają od razu (także na pracującym skanerze); suwaki są
zapisywane z krótkim opóźnieniem (debounce).

### 14.1 Strona Nasłuch

- Duży odczyt częstotliwości głównego slotu (zielony przy sygnale), nazwa z
  listy pamięci, pozostałe aktywne kanały jako "chipy", stan skanera, miernik
  poziomu dBFS z markerem progu wyzwalania, "czas powrotu do tego okna".
- Przyciski: Start/Stop, Słuchaj (+ głośność), Pomiń, Pomiń + czarna lista,
  Nagrywaj (nagrywanie ręczne, §12), przełącznik automatycznego nagrywania,
  Surowe IQ.
- Widmo bieżącego okna przechwytu z linią progu i wodospadem (czyszczonym
  przy zmianie okna).
- "Co skanować": lista pasm (włącz/wyłącz, nazwa, zakres/krok/modulacja,
  waga), "skanuj tylko to pasmo" (§6.6), edycja z **własnymi ustawieniami
  pasma** — każde nadpisanie (próg, czasy, tryb i wartość wzmocnienia,
  redukcja szumów) ma przełącznik "własna wartość / globalnie" (§7.4);
  dodawanie z gotowych presetów, własnego zakresu albo pojedynczej
  częstotliwości; przełącznik całej listy pamięci.
- Panel "Odbiornik": próg wyzwalania, blokada szumów (+ offset), tryb
  wzmocnienia (ręczny / sprzętowy auto / programowy auto) i wartość, czas
  strojenia, podtrzymanie po transmisji, redukcja szumów (+ siła).
- Urządzenie SDR i szerokość przechwytu — zablokowane podczas pracy skanera.
- Dziennik zdarzeń silnika.

### 14.2 Strona Kanały

- **Lista pamięci**: tabela pogrupowana folderami (zwijanymi), wyszukiwanie,
  dodawanie/edycja (częstotliwość, nazwa, modulacja, folder, waga),
  włącz/wyłącz i waga bezpośrednio w tabeli, foldery (nowy, zmiana nazwy,
  usunięcie z zawartością), dodanie polskich częstotliwości wojskowych i
  strażackich (§14.3) do własnych folderów (bez duplikatów).
- **Czarna lista**: wyszukiwanie, ręczne dodanie (częstotliwość, margines,
  opis), usunięcie; wpisy z "Pomiń + czarna lista" są tu widoczne.

### 14.3 Gotowe presety (znane publicznie częstotliwości)

Presety pasm (Ustawienia → Presety pasm) i zestawy kanałów wojskowych/
strażackich pochodzą z publicznie dostępnych źródeł. Presety: wbudowane +
własne; edycja wbudowanego tworzy własną kopię o tym samym identyfikatorze,
usunięcie wbudowanego go ukrywa; import nigdy nie nadpisuje (nowy
identyfikator przy kolizji); eksport tylko własnych.

### 14.4 Strona Nagrania

- Panel boczny: Częstotliwości / Pasma / Języki, każda pozycja z liczbą
  nagrań i średnią jakością; filtr, sortowanie; "Wszystkie", "Ulubione";
  Ctrl+klik wybiera kilka pozycji.
- Wyszukiwanie po częstotliwości, nazwie, dacie, nazwie pliku i treści
  transkrypcji; zakres czasu, minimalna jakość, kolejność (najnowsze,
  najstarsze, najlepsze, najdłuższe); czas lokalny/UTC; automatyczne
  odtwarzanie kolejnego; przełącznik automatycznej transkrypcji.
- Lista stronicowana (doładowywana przy przewijaniu), zaznaczanie wielu (także
  "zaznacz wszystkie pasujące") z akcjami: ulubione, transkrybuj, usuń.
- Odtwarzacz: fala dźwiękowa (§14.5) z przewijaniem kliknięciem, poprzednie/
  następne (także klawisze ↑/↓, spacja), ulubione, zapis częstotliwości na
  listę pamięci (nazwa + folder), odtworzenie lotów FR24/ADSBx (§17.5),
  pobranie pliku, usunięcie; transkrypcja z językiem, oznaczeniem niskiej
  pewności/edycji, ręczną edycją i ponowną transkrypcją (z wymuszonym
  językiem lub wykrywaniem).
- Nowe nagrania pojawiają się same, gdy przeglądane są najnowsze.

### 14.5 Widok fali dźwiękowej

Zgrubny przegląd obwiedni nagrania (§17.4), z zaznaczonym postępem
odtwarzania; kliknięcie przewija.

---

## 15. Strona Statystyki

Kafelki (liczba nagrań, łączny czas, liczba częstotliwości, średnia jakość,
najaktywniejsza częstotliwość, zakres dat), wykresy wg godziny i dnia
tygodnia, dzienny (ostatnie dni), tabela częstotliwości (liczba, jakość,
łączny czas, ostatnio słyszana) — kliknięcie otwiera nagrania tej
częstotliwości. Czas lokalny lub UTC.

---

## 16. Strona Ustawienia

Nagrywanie (automatyczne, limit jednoczesnych, WAV/MP3, folder); Dysk i
przechowywanie (§21.3); Transkrypcja i komputery (§21.2, modele Whisper,
urządzenie, języki, minimalna długość); Połączenie z serwerem (tylko w trybie
lokalnym, §21.1); Dostęp (opcjonalne hasło, §21.4); Lokalizacja (do FR24/ADSBx,
także z lokalizacji urządzenia); Harmonogram (recency boost, domyślny margines
czarnej listy); Presety pasm; Wygląd i język ("automatycznie" = język
przeglądarki); Zaawansowane (tryb debugowania z symulowanym urządzeniem,
przywrócenie ustawień domyślnych — zachowuje nagrania, folder nagrań, hasło i
podłączone komputery; w trybie lokalnym zamknięcie programu).

---

## 17. Biblioteka nagrań (logika danych)

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
transkrypcji/statusu ulubionego z pamięci podręcznych. Biblioteka **nigdy nie
pokazuje pliku, który jest jeszcze zapisywany** (otwarte sloty i nagranie
ręczne są pomijane; konwersja do MP3 zapisuje pod tymczasową nazwą i podmienia
na końcu) — pokazany plik może zostać pobrany, skopiowany, przetranskrybowany
albo usunięty. Pełne przeskanowanie katalogu odbywa się co ~2 minuty (pliki
dodane z zewnątrz), analiza jakości brakujących plików — w tle.

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

### 18.6 Kolejka — kto i co transkrybuje

- Transkrypcję wykonuje dowolny "komputer" (§21.2), który ma ją włączoną i
  zainstalowaną — sam program albo komputer pomocniczy z GPU.
- Komputer prosi o zadanie; dostaje najpierw ręcznie zlecone (z ewentualnie
  wymuszonym językiem), potem — gdy włączona jest automatyczna transkrypcja —
  **najnowsze** nagranie bez wyniku. Zadanie jest "dzierżawione" (np. 15 min):
  dwa komputery nigdy nie robią tego samego pliku.
- Wynik dla pliku, który w międzyczasie zmieniono lub usunięto (inny klucz
  ścieżka|rozmiar|czas modyfikacji), jest odrzucany.
- Plik zakończony błędem nie wraca do kolejki automatycznie do restartu
  programu (zabezpieczenie przed nieskończoną pętlą); ręczna "ponowna
  transkrypcja" zawsze działa.
- Zapis pamięci podręcznej transkrypcji jest throttlowany (najwyżej co ~5 s).

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

---

## 21. Tryby pracy, komputery pomocnicze, dysk i dostęp

### 21.1 Trzy tryby jednego programu

- **Lokalny** (domyślny): ten komputer skanuje; interfejs pod
  `http://127.0.0.1:8765`, otwierany automatycznie; ponowne uruchomienie tylko
  otwiera działający interfejs. Zamykany z Ustawień → Zaawansowane.
- **Serwer** (`--server`, usługa systemd): to samo, ale dostępne z całej sieci;
  skanowanie startuje od razu (`--start`). Zatrzymanie usługi (np. przez
  przełącznik trybów dongla) zamyka nagrania poprawnie.
- **Klient**: komputer połączony z serwerem (Ustawienia → Połączenie z
  serwerem) nie skanuje; pracuje dla serwera jako komputer pomocniczy (§21.2)
  i otwiera interfejs serwera. Lokalna strona pokazuje stan połączenia i
  pozwala wrócić do trybu lokalnego. Każda zmiana trybu najpierw poprawnie
  zamyka skaner i nagrania, potem uruchamia program ponownie.

### 21.2 Komputery pomocnicze

Każdy proces programu ma wbudowany "komputer" (sam serwer), a każdy klient
jest kolejnym. Na serwerze, per komputer: transkrypcja tu (z przyciskiem
instalacji Whisper, także z GPU), trzymanie kopii każdego nagrania w
wybranym folderze (kopia uznawana za gotową dopiero po sprawdzeniu rozmiaru;
pobieranie przez plik tymczasowy), odłączenie, zapomnienie. Polecenia
dochodzą do komputera w ciągu kilku sekund. Przy pierwszym połączeniu klient
jednorazowo przenosi swoje dotychczasowe transkrypcje, oceny jakości i
ulubione dla plików, które serwer też ma (dopasowanie po ścieżce względnej i
rozmiarze; nigdy nie nadpisuje). Jeśli klient ma nagrania, których serwer nie
ma, serwer pokazuje ich liczbę i jednym przyciskiem ściąga je do siebie
(razem z metadanymi; u klienta zostają).

### 21.3 Dysk i przechowywanie

Strona pokazuje zajętość (nagrania na tle dysku), liczbę plików, najstarsze
nagranie i ile ma kopię na innym komputerze. Automatyczne czyszczenie (co 10
minut, domyślnie wyłączone): usuwanie starszych niż N dni i/lub utrzymywanie
biblioteki poniżej X GB (najpierw najstarsze); opcjonalnie nigdy nie usuwaj
ulubionych, opcjonalnie usuwaj tylko nagrania skopiowane na inny komputer.
Podgląd pokazuje, ile nagrań przekracza limity, z przyciskiem "Wyczyść teraz".

### 21.4 Dostęp

Domyślnie bez hasła — każdy, kto dotrze do adresu w sieci, może korzystać.
Opcjonalne hasło (Ustawienia → Dostęp) obowiązuje wtedy wszystkie przeglądarki
i komputery pomocnicze (ciasteczko HttpOnly / nagłówek Bearer). Niezależnie od
hasła: każde żądanie zmieniające stan wymaga własnego nagłówka (inne strony
WWW nie mogą wywołać akcji), a tryb lokalny bez hasła odrzuca obce nazwy
hosta (ochrona przed DNS rebinding).
