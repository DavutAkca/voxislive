# Voxis Live

**[English](README.md)** | **[Türkçe]** | **[Deutsch](README.de.md)**

![Platform](https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-0078D6?logo=windows&logoColor=white)
![Python](https://img.shields.io/badge/python-3.11--3.13-3776AB?logo=python&logoColor=white)
![License](https://img.shields.io/badge/license-PolyForm%20Noncommercial%201.0.0-blue)
[![GitHub stars](https://img.shields.io/github/stars/DavutAkca/voxislive?style=social)](https://github.com/DavutAkca/voxislive/stargazers)

> Windows için gerçek zamanlı sesli çeviri — herhangi bir videoyu, oyunu veya toplantıyı çevirin ve kendi dilinizde, anlık olarak duyun.
>
> Marka: **Voxis** · Site: **[voxislive.com](https://voxislive.com)**

<!-- TODO(owner): bir sonraki push'tan önce (Show HN / Product Hunt) buraya kısa bir GIF veya ekran görüntüsü ekle —
     görselsiz metin duvarı GitHub'ın 3 saniyelik kaydırma testini kaybeder. -->

**📖 Kılavuz:** [Geliştirici / BYOK kurulumu](docs/INSTALL_BYOK.md) — son kullanıcı uygulaması **Microsoft Store** üzerinden dağıtılır; kurulum dökümanı [voxislive.com](https://voxislive.com)'da.

> [!WARNING]
> **Sadece [voxislive.com](https://voxislive.com), Microsoft Store listesi veya bu repo (`github.com/DavutAkca/voxislive`) üzerinden indirin.** Bu reponun başka GitHub hesaplarında kopyaları tespit edildi; bazıları başka sitelerdeki yükleyicilere yönlendiriyor — bunlar **resmi değildir** ve zararlı olabilir. Bu proje hiçbir zaman üçüncü taraf bir siteden veya farklı bir GitHub hesabından yükleyici dağıtmaz. Şüpheli bir kopya görürseniz bildirin: [support@voxislive.com](mailto:support@voxislive.com).

---

## Genel Bakış

Tarayıcı-sekmesi dublajcıları yalnızca tek bir Chrome sekmesinde çalan sesi çevirebilir. Voxis **Windows sistem sesini doğrudan** okur — native oyunlar, masaüstü Zoom/Teams/Discord görüşmeleri, herhangi bir yerel video oynatıcı — yani yalnızca bir sekmede açık olanı değil, PC'nizin çaldığı her şeyi çevirir.

Voxis, Windows sistem sesinizi (bir video, bir oyun, görüşmenin karşı tarafı) yakalar, bunu Google'ın **Gemini Live** çeviri modeline aktarır ve hedef dilinizdeki sesli çeviriyi — daha konuşma devam ederken — geri oynatır.

`gemini-3.5-live-translate-preview` modelini kullanır; bu **yerel, eşzamanlı (simültane) konuşmadan konuşmaya** çalışan bir modeldir: konuşmacı konuştukça sürekli çeviri yapar, kalite ile senkronizasyon arasında kendi kendini dengeler ve (tıpkı bir simültane çevirmenin yaptığı gibi) birkaç saniye geriden gelir. Ayrı bir konuşmadan metne → çeviri → metinden konuşmaya zinciri yoktur; ses girer, çevrilmiş ses çıkar.

İki çalışma modu:

- **Video / Oyun** — tek yönlü gelen çeviri; çeviri konuşurken orijinal ses kısılır (ducking).
- **Toplantı** — çift yönlü: karşı tarafın sesi sizin dilinize çevrilir (kulaklığınıza), sizin sesiniz de onların diline çevrilip sanal bir mikrofon olarak görüşmeye verilir.

Her oturum **TXT / SRT / VTT** (iki dilli altyazı) olarak kaydedilip dışa aktarılabilir ve geçmiş oturumlar uygulama içi Geçmiş panelinde aranabilir.

---

## Nasıl çalışır

```
Windows audio ──► Capture ──► Silero VAD gate ──► Gemini Live (translate) ──► Player ──► Headphones
                (loopback /     (filters non-                                 (limiter,
                 VB-CABLE)        speech)                                      stereo mix)
```

- **Yakalama (Capture)** — iki yol:
  - *Sürücüsüz* (varsayılan, kurulum yok): WASAPI process-exclude loopback (Windows 10 2004+) sistem miksini okur ve Voxis'in kendi çıkışını hariç tutar; böylece kendi sesini asla yeniden çevirmez. Diğer uygulamalar, Windows oturum-ses seviyesi (session-volume) API'si üzerinden kaynağında kısılır.
  - *VB-CABLE*: ses, hoparlörlere ulaşmadan önce yakalanır; böylece motor gerçek DSP uygulayabilir — M/S merkez bastırma (center-suppression) orijinal diyaloğu kısarken stereo müziği korur ve kesirli bir gecikme hattı (delay line) orijinali çeviriyle RTT'ye göre hizalar.
- **VAD geçidi (gate)** — Silero VAD v5 (ONNX, CPU) müziği/gürültüyü eler; böylece buluta yalnızca konuşma ulaşır.
- **Çeviri** — bir `LiveTranslator` iş parçacığı (thread), bir Gemini Live WebSocket oturumunu tutar ve içeri 16 kHz PCM, dışarı 24 kHz çevrilmiş ses akışı sağlar.
- **Oynatma** — ileri-bakışlı (look-ahead) bir brick-wall limiter içeren stereo mikser; çeviri, sanal (phantom) merkeze yerleşir.

---

## Hızlı başlangıç (geliştirici derlemesi)

```powershell
git clone https://github.com/DavutAkca/voxislive.git
cd voxislive
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> **Python 3.11–3.13 (64-bit).** Python 3.14 henüz desteklenmiyor: numpy / onnxruntime için sabitlenen sürümlerde kararlı cp314 wheel'i yok, bu yüzden `pip install` başarısız olur.

Çalıştırın:

```powershell
python main.py            # GUI
```

Açık kaynak derleme **BYOK**’tur (kendi anahtarınızı getirin). İlk açılışta
**Ayarlar → API anahtarı** bölümünden Gemini anahtarınızı
(<https://aistudio.google.com/>) yapıştırıp kaydedin; anahtar düz metin bir
`.env` yerine `profiles/byok` altında **şifreli** saklanır (Windows DPAPI ile,
Windows hesabınıza bağlı). Ayrıntılar: [docs/INSTALL_BYOK.md](docs/INSTALL_BYOK.md).

Ses cihazlarınızı istediğiniz zaman `python -m app.audio_io` ile listeleyin.

---

## Derleme türleri — `IS_OFFICIAL_RELEASE`

Voxis iki türde gelir; bunlar derleme sırasında `IS_OFFICIAL_RELEASE` ile seçilir (ortam değişkeni `VOXIS_OFFICIAL_RELEASE=1/0`, varsayılan `False`).

| | Resmi SaaS `.exe` (`True`) | Açık kaynak / geliştirici (`False`) |
| --- | --- | --- |
| API anahtarı | Her oturum için sunucudan alınır; anahtar arayüzü yok | Kendi anahtarınız (BYOK), Ayarlar'da girilir |
| Çeviri motoru | Google Gemini Live + OpenAI, hedef dile göre yönlendirilir | Yalnızca Google Gemini Live |
| Kimlik doğrulama | Oturum açma (PocketBase) | Yok — yerel, çevrimdışı |
| Telemetri / faturalama | Sunucuya kullanım heartbeat'i | Tamamen devre dışı |
| Çeviri ayarları | En iyi simültane varsayılanlara kilitli | Tüm ayarlar ince ayar için açık |

`start.bat`, `VOXIS_OFFICIAL_RELEASE` değişkenini ayarlamaz; bu yüzden kaynaktan yapılan bir başlatma varsayılan olarak BYOK / geliştirici yolunu kullanır (kendi anahtarınız — sunucu yok, kimlik doğrulama yok). Resmi SaaS `.exe`, donmuş pakete `OFFICIAL` işaretçisini yazan `release.py` tarafından ayrıca üretilir.

**Açık kaynak derlemesinin ağ yüzeyi.** Donmuş (frozen) bir geliştirici derlemesi `OFFICIAL` işaretçisi taşımaz; bu yüzden BYOK'a düşer ve **kendi başına hiçbir giden çağrı yapmaz**: kayıt, giriş, doğrulama, kota, sunucu oturum-anahtarı alma, kullanım heartbeat'i ve tüm telemetri devre dışı bırakılır ya da yerel sahte (mock) yanıtlara sabitlenir. Dokunduğu tek ağ, kendi anahtarınızın açtığı Gemini Live WebSocket'idir. Uygulama içi otomatik güncelleyici yoktur (kaldırıldı; resmi uygulama Microsoft Store üzerinden güncellenir). Genel depo, bir sürüm-hijyen kapısıyla (`scripts/check_release_hygiene.py`, CI'ye ve bir pre-push kancasına bağlı) kapalı-çekirdek yollarından ve canlı sırlardan arındırılmış tutulur.

---

## Toplantı modu kurulumu (çift yönlü çeviri)

**Amaç:** siz Türkçe konuşursunuz → karşı taraf İngilizce duyar; karşı taraf İngilizce konuşur → siz Türkçe duyarsınız.

İki yönün farklı gereksinimleri vardır:

| Yön | Ne yapar | Gereksinim |
| --- | --- | --- |
| **Gelen** (onları kendi dilinizde duyarsınız) | Sistem sesini dinler, çevirir, kulaklığınıza oynatır | **Ek kurulum yok** |
| **Giden** (sesiniz çevrilerek dışarı gider) | Mikrofonunuzu çevirir, sanal bir mikrofonu besler | **Sanal bir mikrofon (VB-CABLE) gereklidir** |

> Windows'ta, bir toplantı uygulamasının (Teams/Zoom/Meet) seçebileceği bir "mikrofon" sunmanın tek yolu sanal bir ses sürücüsüdür — bu yüzden giden yön VB-CABLE gerektirir. Böyle bir sürücü olmadan toplantılar otomatik olarak **yalnızca dinleme** modunda çalışır (onları anlarsınız; sesiniz çevrilmeden dışarı gider).

### 1. VB-CABLE kurun (tek seferlik, ücretsiz)
1. <https://vb-audio.com/Cable/> adresinden indirin.
2. Arşivi açın → `VBCABLE_Setup_x64.exe` dosyasına sağ tıklayın → **Yönetici olarak çalıştır** → **Install Driver** → **yeniden başlatın**.
3. İki cihaz görünür: **CABLE Input** (oynatma) ve **CABLE Output** (kayıt).

### 2. Voxis'i yapılandırın
- Panelde dilleri ayarlayın: **Duyduğum dil: Türkçe**, **Karşı tarafa: İngilizce**.
- Ayarlar → **Çıkış cihazı**: gerçek kulaklığınız · **Mikrofon**: gerçek mikrofonunuz — konuştuğunuz mikrofon; Voxis burayı dinler.
- **Sanal kablo otomatik algılanır.** Voxis, başlatıldığında kurulu bir kabloyu (VB-CABLE / VB-Audio / VoiceMeeter) bulur ve toplantı yönlendirmesini kendi yapar — `config.json` düzenlemeye gerek yok.

### 3. Toplantı uygulamasını yapılandırın (Teams / Zoom / Meet)
- **Mikrofonu** **"CABLE Output (VB-Audio Virtual Cable)"** olarak ayarlayın — kablonun *kayıt* tarafı (`CABLE Output`, **`CABLE Input` değil**). Bu, Voxis'te seçtiğiniz gerçek mikrofon değil, toplantı uygulamasının mikrofonudur: Voxis çevrilmiş İngilizceyi kabloya yazar, toplantı uygulaması da buradan okur.
- Birden fazla sanal kablo kuruluysa (örn. VB-Audio Point, VoiceMeeter), **VB-Audio Virtual Cable** çiftini seçin — Voxis'in varsayılan olarak otomatik bağladığı budur.
- Hoparlör/çıkışı kendi kulaklığınız olarak bırakın.

### 4. Kullanın
Voxis'i başlatın → **Toplantı** modu (`Ctrl+Alt+2`). Türkçe konuşun → İngilizce olarak dışarı gider; onlar İngilizce konuşur → siz Türkçe duyarsınız.

---

## Gecikme ve simültane çeviri

Uçtan uca gecikme kabaca **cümle uzunluğu artı birkaç saniyedir** — bu gecikme, çeviri modelinin tasarlanmış *kulak-ses açıklığıdır* (doğru çevirmek için yeterli bağlamı bekler, tıpkı bir insan çevirmenin yaptığı gibi) ve **istemciden ayarlanamaz**. Google tarafında bir "daha hızlı çalış" ayarı yoktur ve bu, mevcut en güncel ve tek çeviri modelidir.

Voxis'in istemci tarafında *gerçekten* optimize ettiği şeyler: modele sürekli bir akış besler (modelin belgelenmiş yerel kurulumu — istemci tarafı uç-nokta yapılandırması gönderilmez), yakalamadan önce bağlantıyı ısıtır; böylece ilk cümle soğuk el sıkışmayı (cold handshake) atlar, WebSocket sıkıştırmasını devre dışı bırakır, küçük bir en-eskiyi-bırakan (drop-oldest) giriş arabelleği tutar ve VAD'ı CPU'da çalıştırır. Bunlar kontrol edilebilir uçları kırpar — modelin asıl gecikmesini değil.

---

## Yapılandırma başvurusu

`config.json` (gitignore'da; varsayılanlar `app/config.py` içinde bulunur):

| Anahtar | Anlamı |
| --- | --- |
| `target_language_incoming` / `target_language_outgoing` | Sizin diliniz / karşı tarafın dili |
| `capture_backend` | `"driverless"` (WASAPI loopback) veya `"vbcable"` |
| `original_audio` | `"duck"` · `"mute_during_speech"` · `"mix"` |
| `duck_gain` | Çeviri konuşurken orijinal sesin seviyesi (0–1) |
| `quality_preset` | `max_quality` · `balanced` · `max_savings` · `turbo` |
| `gemini_voice` / `gemini_temperature` | Hazır ses · örnekleme sıcaklığı (temperature) |
| `tts_volume` | Çeviri oynatma seviyesi |
| `session_rotate_minutes` | Canlı oturum rotasyonu (15 dakikalık tavandan önce) |

**Kalite ön ayarları** modele gönderilen sürekli akışı şekillendiren yerel VAD geçidine eşlenir. `max_savings` ("Saver"), akışa geçit uygular — yalnızca konuşma gönderilir, sessizlik boşlukları atılır — böylece daha az faturalanan dakika kullanılır. Resmi derleme dört kullanıcı dostu seçenek sunar (**Smooth** = `balanced`, **Fast** = `turbo`, **Callout** = `callout`, **Saver** = `max_savings`); geliştirici derlemesi tüm ön ayar listesini açar (`max_quality`, `balanced`, `max_savings`, `turbo`).

Çeviri modeli yerel (native) simültane bir çevirmendir; bu nedenle istemci hiçbir uç-nokta yapılandırması göndermez — sürekli akış besler ve uç-noktalamayı modelin kendisine bırakır.

**Arayüz dilleri** (uygulama arabirimi) **16 yerel dili** kapsar — `ui_language` ile ayarlanır. **Çeviri hedef dilleri** (modelin *neye* çevireceği) bundan bağımsızdır ve **79 dili** kapsar (`tr, en, es, fr, de, it, pt, ru, ar, zh-Hans, ja, ko, hi, …`), `target_language_incoming` / `target_language_outgoing` ile ayarlanır.

---

## Mimari (modül haritası)

| Modül | Sorumluluk |
| --- | --- |
| `app/config.py` | Yapılandırma yükleme/kaydetme, `DEFAULTS`, `QUALITY_PRESETS`, `IS_OFFICIAL_RELEASE`, geçit yardımcıları |
| `app/audio_io.py` | Cihaz keşfi, loopback yakalama, `Player` (stereo miks + limiter), sanal kablo algılama |
| `app/process_loopback.py` | Process-exclude WASAPI loopback (sürücüsüz) |
| `app/session_duck.py` | Windows oturum-ses seviyesi API'si ile kaynak düzeyinde kısma (ducking) |
| `app/vad.py` | Silero VAD (CPU) + `SpeechGate` |
| `app/translator.py` | `LiveTranslator` — Gemini Live oturumu, yerel simültane çeviri, rotasyon |
| `app/pipeline.py` | `IncomingPipeline`, `OutgoingPipeline`, `ModeController` |
| `app/mix_core.py` / `app/dsp.py` | İleri-bakışlı limiter, gecikme hattı, M/S merkez bastırma |
| `app/byok_store.py` | DPAPI ile şifrelenmiş yerel anahtar depolama (geliştirici derlemesi) |
| `app/voxis_client.py` | Auth-core HTTP istemcisi (resmi derleme) |
| `app/webui.py` + `app/web/index.html` | pywebview köprüsü + tek dosyalık arayüz |

İsteğe bağlı bir `premium/` paketi (açık-çekirdek kancası, gitignore'da) ONNX vokal/enstrüman ayrıştırması sağlayabilir; bulunmadığında deterministik M/S merkez bastırma yedeği (fallback) kullanılır.

SaaS arka ucu (`voxislive.com` üzerinde Caddy arkasında bir Go + PocketBase servisi) oturum başına anahtarlar verir ve kullanımı kaydeder; açık kaynak derleme ona asla bağlanmaz.

---

## Sorun giderme

| Belirti | Neden | Çözüm |
| --- | --- | --- |
| `API key not valid` | Geçersiz/boş anahtar (BYOK) veya geliştirici derlemesini anahtarsız çalıştırmak | Ayarlar'da tam bir Gemini anahtarı girin veya sunucu anahtarını kullanmak için `VOXIS_OFFICIAL_RELEASE=1` ile başlatın |
| Toplantı yalnızca dinleme modunda | Kurulu sanal mikrofon yok | VB-CABLE kurun (yukarıya bakın) |
| `PaError -9999` | Eskimiş WASAPI cihaz listesi | USB ses cihazını çıkarıp tekrar takın, yeniden başlatın |
| Hiçbir çeviri çıkışı yönlendirilmiyor | Çıkış bir sanal kabloya ayarlı (geri besleme döngüsü) | `headphones_output` ayarını gerçek cihazınıza yönlendirin |

---

## Lisans — PolyForm Noncommercial 1.0.0

**PolyForm Noncommercial License 1.0.0** altında lisanslanmıştır; tam metin [LICENSE](LICENSE) dosyasındadır.

- ✅ Kişisel, hobi, araştırma ve ticari olmayan amaçlarla kullanım serbesttir.
- ❌ Ticari kullanım, yeniden satış, white-label ve gelir elde etmeye yönelik dağıtım yasaktır.

**Ticari lisanslama** (ticari ürün, SaaS, white-label): **<https://voxislive.com/licensing>**.

Katkılar memnuniyetle karşılanır — bir pull request açarak, katkınızın aynı lisans koşulları altında lisanslandığını ve proje geçmişinde atıfla birlikte dahil edilebileceğini kabul etmiş olursunuz.

---

## Destek

- **Sorunlar:** [GitHub Issues](https://github.com/DavutAkca/voxislive/issues)
- **Ticari talepler:** <https://voxislive.com/licensing>

*Voxis Live — gerçek zamanlı, simültane sesli çeviri.*
