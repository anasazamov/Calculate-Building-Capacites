# HISOBOT: hajmlarni hujjatdan o'qish sinovi

Sana: 2026-11-24. Server: 1× NVIDIA RTX PRO 6000 Blackwell (96 GB). Topshiriq: `TASK.md`.

Bu hisobotda **tekshirilgan fakt** va **taxmin** ajratib yozilgan: "o'lchandi", "tekshirildi"
degan joylar shu serverda bajarilgan sinov natijasi; taxminlar 7-bo'limda, tekshirilmagan
narsalar 8-bo'limda.

## 1. Qisqa xulosa

**Savol:** shu serverdagi ochiq modellar hajmlarni hujjatdan ishonchli o'qiy oladimi?

**Javob:** ha, lekin faqat kod tekshiruvi va xodim tasdig'i bilan birga. 5 obyekt, 8 PDF,
221 etalon qator bo'yicha yakuniy o'lchov:

| Model | To'g'ri | **Jim xato** | Belgi bilan | Qisman | Ushlandi | Topilmadi | O'qilmaydi |
|---|---|---|---|---|---|---|---|
| Qwen3.8-27B-FP8 (1-model) | 201 | **0** | 1 | 2 | 6 | 1 | 10 |
| Gemma-4-31B-it FP8 (2-model) | 205 | **0** | 1 | 0 | 2 | 3 | 10 |
| Granite 4.1 30B FP8 (nomzod)* | 137 | **1** | 0 | 1 | 21 | 51 | 10 |

\* Granite oldingi prompt versiyasida bir marta o'lchangan; qayta o'lchanmagan.

- **Jim xato (tizim qabul qilgan, aslida noto'g'ri qiymat): Qwen — 0, Gemma — 0.** Tizim
  o'qiydigan 211 maydondan Qwen 201 tasini (95,3%), Gemma 205 tasini (97,2%) to'g'ri qabul qildi;
  qolganlari "tekshirilmadi", "ziddiyat" yoki "topilmadi" bo'lib, xodimga ko'rsatiladi.
- Bino bo'yicha Hb etalonining baholangan 21 qiymatidan Gemma 21 tasini, Qwen 20 tasini to'g'ri
  berdi (bittasi "ziddiyat" bo'lib ushlandi); noto'g'ri qabul qilingani yo'q.
- Bu natija darhol chiqmagan: dastlabki uch o'lchovda jim xatolar bor edi (Qwen 4 → 2 → 1,
  Gemma 3 → 0 → 1). Ular model almashtirib emas, kodga tekshiruv qo'shib yo'qotildi (3.3 va
  4-bo'limlar). Oxirgi uch o'lchovda ikkala modelda ham 0.
- Bitta juftlik (40 sahifali albom + 7 sahifali xulosa) ikki model bilan **5 daqiqa 50 soniya**da
  o'qiladi.

- Model o'zi "ishonchli" emas: sinov davomida modellar raqam oldiga `>=` qo'shib yubordi,
  takrorlanish halqasiga tushdi, ustunlarni adashtirdi, hujjatdagi ikki xil yozuvdan birini
  tanladi. Bularning hammasini **kod tekshiruvlari** ushladi (4-bo'lim). Tekshiruvsiz
  foydalanib bo'lmaydi.
- Asosiy o'quvchi sifatida **Qwen3.8-27B-FP8**, ikkinchi mustaqil o'quvchi sifatida
  **Gemma-4-31B-it (FP8)** tavsiya qilinadi. Uchinchi nomzod **Granite 4.1 30B** ancha kuchsiz
  chiqdi va tavsiya qilinmaydi.
- Ha, Ht, Hu, Hyu uchun etalon yo'q (hujjatlarda ular hajm sifatida yozilmagan), shuning uchun
  bu to'rt hajm bo'yicha tizim faqat "hisoblangan, xodim tasdiqlashi kerak" taklifini beradi;
  aniqligi **o'lchanmagan**.

## 2. Nima ishlaydi

| Qism | Holat |
|---|---|
| Hisob yadrosi (`feecalc/fee.py`, Decimal) | ishlaydi; 4 ta nazorat misoli so'mgacha mos; 0,01 chegarasi; manfiy hajm — xato |
| Qabul qilish (PyMuPDF: matn, koordinata, PNG, jadval va shtamp ajratish) | ishlaydi |
| Varaq turi (avval qoidalar, noaniq bo'lsa model) | ishlaydi |
| O'qish (loyiha va xulosa alohida so'rovlarda; ko'p binoli obyektlar, soni, havola, hudud balansi) | ishlaydi |
| Manba tekshiruvi (parcha sahifada so'zma-so'z; son parchadan ajraladi; parchada yorliq bor) | ishlaydi |
| Xonalarni toifalash (lug'at, lug'atda yo'g'ini model taklif qiladi) | ishlaydi; aniqligi o'lchanmagan |
| Hajmlar: Hb binolar bo'yicha qatorlar; Ha/Ht/Hu maydon × balandlik; Hyu taklifi | ishlaydi; Hb etalon bilan tekshirilgan |
| Tekshiruvlar (yig'indilar, loyiha = xulosa, yaxlitlash farqi, hajm chegarasi, manzil, hudud balansi, 2 model kelishuvi) | ishlaydi |
| Veb-interfeys (yuklash, navbat, tekshirish ekrani, hisob, hisob varaqasi, CSV, audit) | ishlaydi; haqiqiy 284459 juftligida sinab ko'rildi |
| 2-model va ikki model kelishuvi | ishlaydi |
| Nazoratchi (kanareyka, "!!!!" alomati, qayta ishga tushirish, hodisalar jurnali) | ishlaydi; ikki sinov o'tkazildi |
| Sanoat binosi uchun 3 m ko'rsatkichi | ishlaydi (faqat ko'rsatadi, ayirmaydi) |
| OCR | yo'q (sinovga kirmaydi); matnsiz sahifa "matnsiz" deb belgilanadi |

Testlar: 160 ta test o'tdi (pytest; to'liq zanjir yozib olingan model javoblari bilan oflayn
qayta ijro etiladi, GPU kerak emas).

## 3. Sinov natijalari

### 3.1. Hisob yadrosi

TASK.md 6-bo'limidagi to'rtta nazorat misoli yig'im, 20% va 80% bo'yicha aynan mos keldi
(`tests/test_fee.py`). To'rtinchi misol (4 294 208 160 so'm) veb-interfeys orqali ham oxirigacha
qayta chiqarildi (`tests/test_web.py`): yuklash → tasdiqlash → hisob → yakunlash.

### 3.2. Model tutun sinovi (o'lchandi)

50 ta ketma-ket va 16 ta parallel qisqa JSON so'rov, kanareyka oldin va keyin:

| Model | Ketma-ket (50) | Parallel (16) | Ketma-ket tok/s | Parallel tok/s | "!!!!" |
|---|---|---|---|---|---|
| Qwen3.8-27B-FP8 | 50 to'g'ri | 16 to'g'ri | 39,8 | 173,6 | kuzatilmadi |
| Gemma-4-31B-it FP8 | 50 to'g'ri | 16 to'g'ri | 36,6 | 148,0 | kuzatilmadi |
| Granite 4.1 30B FP8 | 50 to'g'ri | 16 to'g'ri | 40,6 | 141,9 | kuzatilmadi |

Buzuq va bo'sh javoblar: yuqoridagi yakuniy sozlamada 0. Lekin yo'lda ikkita jiddiy nosozlik
topildi (4.1 va 4.2-bandlar).

### 3.3. Namunalar bo'yicha aniqlik (o'lchandi)

Etalon: `~/samples/etalon_maydonlar.csv` (221 qator). Baholash: `scripts/score_samples.py`.
Zanjir etalonni o'qimaydi; uni faqat baholash skripti o'qiydi. Toifalar:

- **to'g'ri** — tizim qabul qilgan va etalonga teng;
- **jim xato** — tizim qabul qilgan, lekin etalondan farq qiladi, hech qanday belgi yo'q (**asosiy ko'rsatkich**);
- **belgi bilan** — qabul qilingan va etalondan farq qiladi, lekin yonida "hujjatda boshqacha ham yozilgan" belgisi ko'rsatiladi;
- **qisman** — (faqat manzil) qabul qilingan manzil etalon satrining bir qismi;
- **ushlandi** — model qiymat qaytargan, lekin tekshiruv rad etgan ("tekshirilmadi" yoki "ziddiyat");
- **topilmadi** — tizimda qiymat yo'q;
- **o'qilmaydi** — tizim bunday maydonni umuman o'qimaydi (qamrovdan tashqari).

**O'lchovlar tarixi** (har biri barcha namunalarni to'liq qayta o'qish; orada kod va prompt tuzatilgan):

| O'lchov | Nima o'zgardi | Qwen: to'g'ri / jim xato | Gemma: to'g'ri / jim xato |
|---|---|---|---|
| 1 | birinchi ko'r-ko'rona o'qish | 186 / 4 | 185 / 3 |
| 2 | qator oxiridagi chiziqcha, loyihadagi barcha manzil yozuvlari, balandliklar | 204 / 2 | 201 / 0 |
| 3 | faqat sondan iborat manba rad etiladi; bir bino yozuvlari birlashtiriladi | 202 / 1 | 200 / 1 |
| 4 | qavatlar ziddiyati belgisi; PyMuPDF qulfi | 202 / 0 | 198 / 0 |
| 5 | sath nomlari tahlili, qo'shtirnoq, manzil bo'laklarini birlashtirish | 205 / 0 | 203 / 0 |
| 6 (yakuniy) | PyMuPDF neyron jadval modeli o'chirildi | 201 / 0 | 205 / 0 |

1–3-o'lchovlarda baholash skripti ham o'zgargan (bino moslash, manzilni baholash), shuning uchun
ularning "to'g'ri" soni yakuniy bilan aynan solishtirilmaydi. Bir xil kodda ham natija ±2–4 qatorga
o'zgaradi: model har o'qishda ayrim qiymatlar uchun boshqa parcha tanlaydi va u goh tekshiruvdan
o'tadi, goh "tekshirilmadi" bo'ladi. Jim xato bundan o'zgarmadi (oxirgi uch o'lchovda 0).

**Ikki model birga** (yakuniy o'lchov): Qwen qabul qilgan 201 to'g'ri qiymatdan 199 tasini Gemma ham
aynan shunday o'qidi; 2 tasi Gemma'da boshqacha yoki yo'q bo'lgani uchun "mos emas" deb
ko'rsatiladi (ortiqcha tekshiruv, xato emas).

**Maydonma-maydon, 1-model — Qwen3.8-27B-FP8 (yakuniy o'lchov):**

| Maydonlar guruhi | Etalon | To'g'ri | Belgi bilan | Qisman | Ushlandi | Topilmadi | Jim xato | O'qilmaydi |
|---|---|---|---|---|---|---|---|---|
| Xulosa: raqam, sana, murojaat raqami, qurilish turi | 20 | 20 | 0 | 0 | 0 | 0 | 0 | 0 |
| Xulosa: manzil, buyurtmachi | 9 | 6 | 0 | 1 | 1 | 1 | 0 | 0 |
| Xulosa: qurilish hajmi (bino bo'yicha) | 15 | 15 | 0 | 0 | 0 | 0 | 0 | 0 |
| Xulosa: qurilish maydoni, umumiy va foydali maydon | 35 | 35 | 0 | 0 | 0 | 0 | 0 | 0 |
| Xulosa: qavatlar va xonadonlar soni | 11 | 9 | 1 | 0 | 1 | 0 | 0 | 0 |
| Xulosa: tarx o'lchami | 10 | 10 | 0 | 0 | 0 | 0 | 0 | 0 |
| Xulosa: balandliklar | 27 | 27 | 0 | 0 | 0 | 0 | 0 | 0 |
| Xulosa: hudud ko'rsatkichlari | 27 | 27 | 0 | 0 | 0 | 0 | 0 | 0 |
| Xulosa: «Jami» qatori | 3 | 3 | 0 | 0 | 0 | 0 | 0 | 0 |
| Xulosa: havola (boshqa blokka) | 2 | 2 | 0 | 0 | 0 | 0 | 0 | 0 |
| Xulosa: qamrovdan tashqari (binolar soni, tarx eni/uzunligi, diametr) | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 5 |
| Loyiha: qurilish hajmi (jami, yer usti, yer osti) | 8 | 7 | 0 | 0 | 1 | 0 | 0 | 0 |
| Loyiha: qurilish maydoni, umumiy maydon | 12 | 10 | 0 | 0 | 2 | 0 | 0 | 0 |
| Loyiha: bino nomi, soni, qavatlar, xonadonlar, tarx | 10 | 9 | 0 | 0 | 1 | 0 | 0 | 0 |
| Loyiha: sathlar bo'yicha maydonlar | 5 | 5 | 0 | 0 | 0 | 0 | 0 | 0 |
| Loyiha: sath belgilari | 4 | 4 | 0 | 0 | 0 | 0 | 0 | 0 |
| Loyiha: xona maydonlari | 4 | 4 | 0 | 0 | 0 | 0 | 0 | 0 |
| Loyiha: hudud ko'rsatkichlari | 4 | 4 | 0 | 0 | 0 | 0 | 0 | 0 |
| Loyiha: manzil, shifr | 5 | 4 | 0 | 1 | 0 | 0 | 0 | 0 |
| Loyiha: qamrovdan tashqari (bo'lim, xonadon turlari, bino balandligi, xona nomi) | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 5 |
| **Jami** | **221** | **201** | **1** | **2** | **6** | **1** | **0** | **10** |

**Maydonma-maydon, 2-model — Gemma-4-31B-it FP8 (yakuniy o'lchov):**

| Maydonlar guruhi | Etalon | To'g'ri | Belgi bilan | Qisman | Ushlandi | Topilmadi | Jim xato | O'qilmaydi |
|---|---|---|---|---|---|---|---|---|
| Xulosa: raqam, sana, murojaat raqami, qurilish turi | 20 | 20 | 0 | 0 | 0 | 0 | 0 | 0 |
| Xulosa: manzil, buyurtmachi | 9 | 7 | 0 | 0 | 1 | 1 | 0 | 0 |
| Xulosa: qurilish hajmi (bino bo'yicha) | 15 | 15 | 0 | 0 | 0 | 0 | 0 | 0 |
| Xulosa: qurilish maydoni, umumiy va foydali maydon | 35 | 35 | 0 | 0 | 0 | 0 | 0 | 0 |
| Xulosa: qavatlar va xonadonlar soni | 11 | 9 | 1 | 0 | 1 | 0 | 0 | 0 |
| Xulosa: tarx o'lchami | 10 | 10 | 0 | 0 | 0 | 0 | 0 | 0 |
| Xulosa: balandliklar | 27 | 27 | 0 | 0 | 0 | 0 | 0 | 0 |
| Xulosa: hudud ko'rsatkichlari | 27 | 27 | 0 | 0 | 0 | 0 | 0 | 0 |
| Xulosa: «Jami» qatori | 3 | 3 | 0 | 0 | 0 | 0 | 0 | 0 |
| Xulosa: havola (boshqa blokka) | 2 | 2 | 0 | 0 | 0 | 0 | 0 | 0 |
| Xulosa: qamrovdan tashqari (binolar soni, tarx eni/uzunligi, diametr) | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 5 |
| Loyiha: qurilish hajmi (jami, yer usti, yer osti) | 8 | 8 | 0 | 0 | 0 | 0 | 0 | 0 |
| Loyiha: qurilish maydoni, umumiy maydon | 12 | 12 | 0 | 0 | 0 | 0 | 0 | 0 |
| Loyiha: bino nomi, soni, qavatlar, xonadonlar, tarx | 10 | 10 | 0 | 0 | 0 | 0 | 0 | 0 |
| Loyiha: sathlar bo'yicha maydonlar | 5 | 5 | 0 | 0 | 0 | 0 | 0 | 0 |
| Loyiha: sath belgilari | 4 | 2 | 0 | 0 | 0 | 2 | 0 | 0 |
| Loyiha: xona maydonlari | 4 | 4 | 0 | 0 | 0 | 0 | 0 | 0 |
| Loyiha: hudud ko'rsatkichlari | 4 | 4 | 0 | 0 | 0 | 0 | 0 | 0 |
| Loyiha: manzil, shifr | 5 | 5 | 0 | 0 | 0 | 0 | 0 | 0 |
| Loyiha: qamrovdan tashqari (bo'lim, xonadon turlari, bino balandligi, xona nomi) | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 5 |
| **Jami** | **221** | **205** | **1** | **0** | **2** | **3** | **0** | **10** |

Tushuntirishlar:

- **"Belgi bilan" 1 ta** (ikkala modelda): 291405 qavatlar soni — hujjatning o'zidagi ziddiyat (4.7-band).
- **Qwen "ushlandi" 6 ta:** 302030 loyihasida bir bino ko'rsatkichlari bosh reja chizmalarida
  (8–10-sahifalar, matni tartibsiz) boshqacha o'qilgan va "ziddiyat" bo'lgan (4 ta); 298814 qavatlar
  soni ("Uch qavatli" so'z bilan yozilgan, model "3" qaytargan — son parchadan ajralmadi); 291405
  manzil (qiymat parchada so'zma-so'z emas).
- **"O'qilmaydi" 10 ta:** binolar soni, tarx eni va uzunligi alohida, diametrlar, loyiha bo'limi,
  2 va 3 xonali xonadonlar soni, binoning eng yuqori belgisi, xona nomi. Bular hajm hisobiga
  kirmaydi, shuning uchun sxemaga qo'shilmagan.
- Granite: 302030 da 49 qatordan 7 tasi, 291405 da 19 dan 8 tasi to'g'ri; jim xato — 302030
  xulosasida murojaat raqami o'rniga xulosa raqami.

### 3.4. Bino bo'yicha Hb (o'lchandi)

Etalon: `etalon.csv` (18 qator). Tizim Hb ni binolar bo'yicha qatorlardan yig'adi.

| Ko'rsatkich | Qwen | Gemma | Granite* |
|---|---|---|---|
| Loyihadagi hajm (6 qiymat) | 5 to'g'ri, 1 ushlandi | 6 to'g'ri | 1 to'g'ri, 5 topilmadi |
| Xulosadagi hajm (15 qiymat; 2 tasi havola, 1 tasi soni × birlik) | 15 to'g'ri | 15 to'g'ri | 9 to'g'ri, 2 ushlandi, 4 topilmadi |
| Noto'g'ri qabul qilingan | 0 | 0 | 0 |

Havola holati (302704, Blok №6 va №7): tizim hajmni o'zi qo'shmadi, havolani ko'rsatdi — bu
"to'g'ri" deb sanalgan.

Obyekt bo'yicha Hb taklifi va shartnomada qo'llangan Hb (`shartnoma.csv`, faqat yonma-yon ko'rsatish uchun):

| Obyekt | Tizim taklifi (Qwen, yakuniy o'lchov) | Shartnomada |
|---|---|---|
| 298814 | 2 226,0 m³ (xulosadan; loyiha berilmagan) | 2 226 |
| 302704 | taklif yo'q: Blok №6 va №7 da faqat havola — xodim kiritadi yoki chiqaradi. 1–5-bloklar yig'indisi 146 442,1 m³ | 2 265,0 |
| 302030 | taklif yo'q: hojatxona hajmi loyihada 251,37, xulosada 10,2 — xodim tanlaydi | 20 214,02 |
| 284459 | 124 433,19 m³ (1-tip 2 × 41 071,1 + 2-tip 36 036 + magazin 6 254,99) | shartnoma yo'q |
| 291405 | taklif yo'q: ikkinchi bino (ko'mir ombori) hajmi yozilmagan; asosiy bino 10 640 m³ | shartnoma yo'q |

302704 da tizim yig'indisi va shartnomadagi Hb o'rtasidagi katta farq tizim xatosi emas: tizim
xulosada yozilgan to'liq hajmlarni ko'rsatadi, shartnomada boshqa hajm qo'llangan. Sanoat binosi
uchun 3 m ko'rsatkichi tizimda alohida chiqadi (1-etap: qurilish maydonlari × 3 m = 52 043,7 m³ —
README'dagi nazorat soniga teng); qaysi hajm olinishini xodim hal qiladi.

### 3.5. README 3-bo'limidagi holatlar: ko'rsatildi / ko'rsatilmadi

1-model (Qwen) natijasi bo'yicha, kod tekshiruvlari chiqishi:

| Obyekt | Holat | Natija |
|---|---|---|
| 298814 | 1. Hajm tekshiruvi: 222,6 × 11,6 = 2 582,16 > 2 226 | **ko'rsatildi** (ogohlantirish) |
| 298814 | 2. Hudud balansi yopilmaydi (1 072,36 ≠ 1 400) | **ko'rsatildi** |
| 298814 | 3. Tarx 1,6 × 15,5 = 24,8 m² qurilish maydoniga mos emas | **ko'rsatildi** |
| 302704 | 1. Blok №6, №7: faqat havola, hajm qo'shilmaydi | **ko'rsatildi** |
| 302704 | 2. Blok №3 ko'rsatkichlari ikki sahifada | **to'g'ri o'qildi** (113 500,0, 4-sahifa) |
| 302704 | 3. Qurilish maydonlari yig'indisi 17 347,9 ≠ 17 345,0 | **ko'rsatildi** (farq 2,9) |
| 302704 | 4. Blok nomlari va MFY nomi ikki xil yozilgan | ko'rsatilmadi (bunday tekshiruv yo'q) |
| 302704 | 5. Sanoat: 3 m dan yuqori qism alohida | **ko'rsatildi** (52 043,7 m³ 3 m gacha qism) |
| 302030 | 1. Hudud ko'rsatkichlari loyiha va xulosada teng | **ko'rsatildi** (4 tasi ham "to'g'ri") |
| 302030 | 2. Kottedjlar: 21 × 943,94 = 19 822,74 va 19 822,65 — yaxlitlash farqi | **ko'rsatildi** |
| 302030 | 3. Hojatxona: qurilish maydoni va hajm mos emas — farq | **ko'rsatildi** |
| 302030 | 4. Uch inshoot faqat loyihada | **qisman**: ikkitasi "xulosada yo'q" deb chiqdi, uchinchisining hajmi qabul qilinmadi |
| 302030 | 5. Ro'yxat yig'indisi 2 468,75 ≠ 2 457,84 | yakuniy o'lchovda ko'rsatilmadi (bir bino maydoni "ziddiyat" bo'lgani uchun yig'indi hisoblanmadi); 5-o'lchovda ko'rsatilgan edi (farq 10,91) |
| 302030 | 6. Manzil yozuvi har xil — farq deb chiqmasligi kerak | **farq deb chiqmadi** |
| 284459 | 1. 2-tip loyiha va xulosada teng | **ko'rsatildi** (hajm, maydonlar, qavat, xonadon — "to'g'ri") |
| 284459 | 2. Loyiha ichidagi yig'indilar yopiladi | **ko'rsatildi** |
| 284459 | 3. Manzil: Namangan / Samarqand | **ko'rsatildi**: loyiha ichida "ziddiyat" va xulosa bilan "farq" |
| 284459 | 4. "Jami" qatori — yaxlitlash farqi | **ko'rsatildi** (2,4; 2; 1,19) |
| 284459 | 5. Hudud qismlari 24 014 ≠ 25 457 | **ko'rsatildi** |
| 284459 | 6. To'rt bino, loyiha faqat 2-tip uchun | **ko'rsatildi**: qolganlari "loyiha berilmagan"; 1-tip bitta qator, soni 2 |
| 284459 | 7. +52.420 xonalari 35,1 ≠ 35,8 | ko'rsatilmadi (xonalar o'qilgan, lekin bunday tekshiruv yo'q) |
| 291405 | 1. SOUE albomi: loyihada hajm maydonlari "topilmadi" | **ko'rsatildi** (model hech narsa o'ylab topmadi) |
| 291405 | 2. Hajm birligi "m2" deb xato yozilgan | **ko'rsatildi** (belgi) |
| 291405 | 3. Hajm tekshiruvi | **ko'rsatildi**, lekin boshqa son bilan: model qavatlar sonini 5 deb olgani uchun 645 × 19,8 = 12 771 > 10 640 (README'da 10 836) |
| 291405 | 4. Qurilish osti maydoni ikki xil (723,81 va 645,0) | ko'rsatilmadi (ikkinchi bino maydoni noma'lum bo'lgani uchun yig'indi solishtirilmadi) |

Jami 25 holatdan 20 tasi ko'rsatildi, 1 tasi qisman, 4 tasi ko'rsatilmadi. Bu jadval bitta
o'qish natijasi; ayrim holatlar (302030 №5) o'qishdan o'qishga o'zgargan.

### 3.6. Tezlik (o'lchandi)

| O'lchov | Qiymat |
|---|---|
| Bitta oqim, uzun javob (≈2 300 token) | 37–43 tok/s (Qwen) |
| 8 parallel so'rov | jami 317 tok/s (Qwen) |
| Qisqa JSON so'rov | 0,6–0,7 s |
| **Bitta juftlik**: 284459, 40 sahifali albom + 7 sahifali xulosa, interfeys orqali, **ikki model birga**, bitta kartada | **350 s (5 daq 50 s)** |
| — qabul qilish (matn + PNG) | 7,3 s |
| — varaq turlari | 1,5 s |
| — loyihani o'qish (har modelga 22 so'rov) | 163 s |
| — xulosani o'qish (har modelga 1 so'rov, ≈3 400 token) | 176 s |
| — toifalash | 2,7 s |
| Xuddi shu juftlik PyMuPDF neyron jadval modeli yoqiq holda | 686 s (11 daq 26 s) |
| Faqat xulosali ish (298814, 9 sahifa), 5 namuna parallel o'qilganda | 113–165 s |

Chiqish tokenlari shu juftlikda: Qwen 19 161, Gemma 13 150. Ikki model bitta kartani bo'lishgani
uchun har biri yolg'iz ishlagandagidan sekinroq; ikki kartali serverda tezroq bo'lishi kerak
(**taxmin**, o'lchanmagan).

### 3.7. Yuk sinovi (o'lchandi)

Haqiqiy o'lchamdagi xulosa so'rovi (≈2 300 chiqish tokeni) 1, 8 va 16 parallel nusxada,
1-model (`scripts/load_test.py`, natija `test_results/load_model1.json`):

| Parallel | Devor vaqti | Jami tok/s | Xato | Bir xil so'rovga turli qiymatlar |
|---|---|---|---|---|
| 1 | 62,7 s | 37,2 | 0 | — |
| 8 | 58,8 s | 317,4 | 0 | yo'q (8 javobda qiymatlar bir xil) |
| 16 | 117,6 s | 317,7 | 0 | yo'q (16 javobda qiymatlar bir xil) |

16 ta so'rovda server 8 tasini bir vaqtda bajaradi (`--max-num-seqs 8`), qolgani navbatda
turadi — shuning uchun vaqt ikki baravar. Parallel yukda javob o'zgarishi bu sinovda
kuzatilmadi; baribir har javob xom holda saqlanadi va qayta so'ralmaydi.

### 3.8. Nazoratchi sinovi (o'lchandi)

| Sinov | Nima qilindi | Natija |
|---|---|---|
| A | 1-model serveri `kill -9` bilan o'chirildi | 11 s da aniqlandi, 147 s da qayta tayyor, kanareyka o'tdi |
| B | mijoz kodi orqali "!!!!" javobi imitatsiya qilindi (belgi fayliga yozuv) | 51 s da qayta ishga tushirish boshlandi, 222 s da tayyor |

Hodisalar `logs/incidents.jsonl` da. Haqiqiy "!!!!" nosozligi sinov davomida **bir marta ham
kuzatilmadi**; B sinovi faqat mexanizmni tekshiradi.

Sinovdan keyin nazoratchi chegaralari yumshatildi (kanareyka 120 → 180 s, ketma-ket 2 → 3 xato),
chunki to'liq yuk ostida kanareyka navbatda kutib qolishi mumkin; mijoz parallelligi ham 8 dan
6 ga tushirildi. Yangi chegaralar bilan A/B sinovi **takrorlanmadi**.

## 4. Uchragan nosozliklar

### 4.1. Sxema bilan cheklangan javob buziladi, agar promptda javob ko'rinishi aytilmasa (Qwen)

Birinchi tutun sinovida (o'zbekcha prompt, javob ko'rinishi aytilmagan) 50 so'rovdan faqat 13 tasi
to'g'ri chiqdi: 28 tasida qiymat oldiga belgi qo'shildi (`">=10274.2"`, `"> 10411.3"`), 9 tasi
bo'shliq belgilari halqasi bilan `max_tokens` gacha yetdi. Sabab: model tabiiy holda sonni
qo'shtirnoqsiz yozmoqchi, sxema esa satr talab qiladi; cheklov modelni chalg'itadi.
**Chora:** har promptda javob ko'rinishi aniq yoziladi (shundan keyin 50/50 va 16/16); son
qat'iy tahlil qilinadi — `">=10274.2"` son emas, qabul qilinmaydi (`verify.parse_number`).

### 4.2. Takrorlanish halqasi (Qwen, haqiqiy chizma varag'ida)

302030 loyihasining bosh reja varag'ida (matn qatlami tartibsiz) model `manba_matn` ichida
`"1 | | | | | …"` halqasiga tushdi. TASK.md bo'yicha model kartasidagi sozlama bilan solishtirildi
(shu so'rov, har biridan 4 marta, `test_results/repetition_loop_sampling_comparison.json`):

| Sozlama | Halqa |
|---|---|
| temperature 0,2 / top_p 0,8 / top_k 20 | 4 tadan 2 |
| temperature 0,7 / top_p 0,8 / top_k 20 (model kartasi) | 4 tadan 4 |

Ya'ni 0,7 yaxshiroq emas; 0,2 qoldirildi. **Chora:** JSON sxemada har satrga uzunlik chegarasi
(`maxLength`) qo'yildi — vLLM buni majburlaydi, halqa satr ichida davom eta olmaydi. Shundan
keyingi barcha o'lchovlarda halqa kuzatilmadi. Halqa aniqlansa so'rov xato bilan to'xtaydi,
lekin server qayta ishga tushirilmaydi (bu "!!!!" nosozligi emas).

### 4.3. Nazoratchi keraksiz qayta ishga tushirdi (mening xatom, tuzatildi)

Dastlab takrorlanish halqasi ham "!!!!" alomati deb belgilanar va nazoratchi serverni qayta ishga
tushirib, bajarilayotgan so'rovlarni uzar edi; ikkita ketma-ket belgi yozuvi esa ikki marta
qayta ishga tushirishga olib kelgan. Ikkalasi tuzatildi.

### 4.4. PyMuPDF ko'p oqimda xavfsiz emas (mening xatom, tuzatildi)

Namunalar parallel oqimlarda o'qilganda bir sahifaning matni ikki ishga tushirishda farq qildi.
PyMuPDF bilan ishlaydigan hamma joy bitta qulf ostiga olindi (`ingest.MUPDF_LOCK`).

### 4.5. PyMuPDF jadval ajratgichi katak matnini buzadi

`find_tables().extract()` "600.3" ni "6003 ." qilib qaytardi. Jadvaldan faqat katak chegaralari
olinadi, katak matni so'z koordinatalaridan qayta yig'iladi.

### 4.6. PyMuPDF 2.1 jadval qidirishda yashirin neyron model ishlatadi

`find_tables()` sukut bo'yicha `pymupdf-layout` (ONNX, CPU) modelini chaqiradi: chizma sahifasiga
8–25 s. U o'chirildi (`pymupdf._layout_enabled = False`): o'sha jadvallar 0,7 s da, deterministik
topiladi. Ma'lumot baribir serverdan chiqmagan (model mahalliy), lekin zanjirda nazoratsiz ikkinchi
model bo'lmasligi kerak.

### 4.6a. Ikki model bir vaqtda ishga tushirilsa xotira ulushi buziladi

Ikkala vLLM serveri bir paytda yuklanganda Gemma 0,55 ulush (≈54 GB) o'rniga 81 GB egalladi va
Qwen ishga tushmadi (nazoratchi uni qayta-qayta ishga tushirishga urindi). Ketma-ket ishga
tushirilganda muammo yo'q: Qwen 37,8 GB, Gemma 51,6 GB. **Chora:** `scripts/start_watchdog.sh`
server tayyor bo'lguncha kutadi; modellar birin-ketin ishga tushiriladi (avval 1-model).

### 4.7. Hujjatning o'zidagi ziddiyat (291405)

Xulosaning 2-sahifasida "yerto'la, 4 qavat hamda mansarda", 5-sahifasida "Yerto'lali 5-qavat bino".
Model ikkalasidan birini qaytaradi (bir o'qishda 4, boshqasida 5; oxirgi o'lchovlarda ikkala model
ham 5). Manba tekshiruvi buni ushlamaydi — ikkala yozuv ham hujjatda bor. **Chora:** kod hujjat
matnidan "N qavatli" yozuvlarini o'zi qidiradi va hech bir binoga to'g'ri kelmaydigan son topilsa,
qavatlar soni yoniga "ziddiyat" belgisini qo'yadi. Bu faqat qavatlar soni uchun qilingan; boshqa
maydonlarda shunday ichki ziddiyat bo'lsa, uni faqat ikki model farqi yoki yig'indi tekshiruvi
ko'rsatadi.

### 4.8. Modellar ko'rsatgan boshqa xatolar (hammasi ushlandi)

- Ustunlarni adashtirish: bosh reja chizmasidan "soni" o'rniga "qavati" olindi. Chora: faqat
  sondan iborat manba parchasi qabul qilinmaydi (parchada yorliq yoki qator nomi bo'lishi shart);
  turli varaqlardagi bir bino yozuvlari birlashtiriladi va farq qilsa "ziddiyat" bo'ladi.
- Manzilni qo'shtirnoq joyida bo'lib yuborish. Chora: sahifada yonma-yon turgan bo'laklar sahifa
  matnidan birlashtiriladi.
- Qator oxiridagi chiziqcha ("125-\nuy" → "125-uy"). Chora: solishtirishda chiziqcha atrofidagi
  bo'shliq e'tiborga olinmaydi.
- Gemma 284459 xulosasida ikki marta kelgan 1-tip binoni bitta deb oldi (soni yozilmagan).
  "Jami" qatori tekshiruvi buni farq deb ko'rsatadi; oxirgi ikki o'lchovda bu takrorlanmadi.
- Granite murojaat raqami o'rniga xulosa raqamini qo'ydi (ushlanmagan jim xato) — shu sabab ham
  tavsiya qilinmaydi.

## 5. Versiyalar va ishga tushirish buyruqlari

| Narsa | Versiya |
|---|---|
| GPU, drayver | NVIDIA RTX PRO 6000 Blackwell Server Edition, 97 887 MiB; 580.95.05 |
| vLLM | 0.34.0 (PyTorch 2.14.0+cu132, CUDA 13.2, transformers 5.22.2) |
| 1-model | `Qwen/Qwen3.8-27B-FP8`, snapshot `c3a462cc3d38cf351f3b26cbd763469eeb74ef81` |
| 2-model | `RedHatAI/gemma-4-31B-it-FP8-block`, snapshot `d7242548c457ab4b45bd3adb8937f2659af4739d` |
| nomzod | `ibm-granite/granite-4.1-30b-fp8`, snapshot `aa99937ea2b8acf605601a0789ad2daea26b4c7e` |
| Python, PyMuPDF | 3.12.3; PyMuPDF 2.1 (MuPDF 1.28.6) |
| FastAPI, Uvicorn, Jinja2, httpx | 0.165.0; 0.54.0; 3.1.6; 0.28.1 |
| uv | 0.13.9 |

Docker serverda yo'q, shuning uchun vLLM `uv` bilan alohida muhitga o'rnatildi.

```bash
# modellar (har biri o'z nazoratchisi ostida)
scripts/start_watchdog.sh model1     # 127.0.0.1:8001
scripts/start_watchdog.sh model2     # 127.0.0.1:8002
# veb-interfeys
.venv/bin/python -m feecalc.web      # 127.0.0.1:8000
# sinovlar
.venv/bin/pytest -q
.venv/bin/python scripts/smoke_test.py model1
.venv/bin/python scripts/read_samples.py model1 && .venv/bin/python scripts/score_samples.py model1 model2
.venv/bin/python scripts/load_test.py model1
```

vLLM bayroqlari (`scripts/vllm_model1.sh`): `--reasoning-parser qwen3 --max-model-len 32768
--max-num-seqs 8 --gpu-memory-utilization 0.40`, speculative decoding yoqilmagan, faqat
`127.0.0.1`. 2-model: `--reasoning-parser gemma4 --max-model-len 32768 --max-num-seqs 8
--gpu-memory-utilization 0.55`. So'rov: thinking o'chiq, JSON sxema, `temperature 0.2, top_p 0.8,
top_k 20, presence_penalty 0`, `max_tokens` qat'iy (xulosa 12 000, varaq 6 144). To'liq yo'riqnoma:
`README.md`.

## 6. TASK.md dan chetlashishlar

1. **Bitta 96 GB karta** (TASK.md da 2× RTX PRO 5000 deyilgan). Ikki model bitta kartada:
   Qwen 0,40 ulush (≈39 GB, talab 38 GB), Gemma 0,55 ulush. `--gpu-memory-utilization 0.90`
   o'rniga shu ulushlar. Gemma 32 768 kontekst uchun 15,8 GiB KV-kesh talab qildi — 0,45 va 0,50
   ulushda ishga tushmadi.
2. **Kod, fayl nomlari, izohlar va commit xabarlari ingliz tilida** (sizning ish davomidagi
   ko'rsatmangiz); interfeys va xodimga ko'rinadigan xabarlar o'zbekcha.
3. **Kalit nomlari inglizcha:** `{value, unit, page, source_text}` (TASK.md dagi
   `{qiymat, birlik, sahifa, manba_matn}` o'rniga).
4. **Ko'p binoli obyekt.** TASK.md bitta bino misolida yozilgan; namunalarda 7 blokli zavod,
   21 kottedj, 4 binoli majmua bor. Shuning uchun Hb binolar bo'yicha qatorlardan yig'iladi,
   loyiha va xulosa binolari juftlanadi (avtomatik taklif, xodim o'zgartira oladi).
5. **Loyiha ixtiyoriy.** 298814 va 302704 da loyiha yo'q: faqat xulosa o'qiladi, Hb xulosadan
   taklif qilinadi va "loyiha berilmagan" deb belgilanadi.
6. **GitHub'ga push qilinmadi:** serverda GitHub hisob ma'lumoti yo'q
   (`could not read Username for 'https://github.com'`). Barcha commitlar lokal `main` da va
   arxivda. Push uchun token yoki SSH kalit kerak.
7. **Uchinchi model** (Granite) sizning "boshqa modellarni ham sina" ko'rsatmangiz bo'yicha
   qo'shildi. Modelni "noldan qayta qurish" qilinmadi: bu ijara vaqtida bajarib bo'lmaydi va
   topilgan nosozliklar model arxitekturasida emas, kirish matni va cheklanmagan sxemada edi.

## 7. Qilingan taxminlar

1. `~/samples.zip` — siz yuborgan namunalar; `~/samples/` ga ochildi (nazorat yig'indilari mos).
2. Koeffitsiyentlar matni `TASK.md` va ma'lumotnoma jadvalidan olingan; qarorning rasmiy matni
   bilan **solishtirilmagan**.
3. Yakuniy summa butun so'mgacha ROUND_HALF_UP — bu TASK.md qoidasi; nizomda yaxlitlash tartibi
   yozilmagan.
4. 0,01 chegarasi koeffitsiyentlar ko'paytmasiga qo'llanadi (TASK.md bo'yicha). Muqobil talqin —
   summa ≥ 0,01 × BHM.
5. Hyu Hb ga 1 stavka bilan qo'shiladi (formula bo'yicha); qarorning 5-bandidagi 0,5–1 oralig'i
   hisobga olinmagan. Boshqacha kelishuv bo'lsa xodim Hyu ni mos ulushda kiritadi.
6. Hb qatorida loyiha va xulosa mos kelsa loyihadagi qiymat olinadi; loyiha bo'lmasa xulosadagi
   (soni × bir dona hajmi); mos kelmasa yoki faqat havola bo'lsa taklif berilmaydi — xodim hal qiladi.
7. Hajm yozilmagan xona uchun hajm = maydon × sath balandligi; sath aniqlanmasa va hujjatda yagona
   qavat balandligi bo'lsa, o'sha olinadi va "tasdiqlang" deb belgilanadi.
8. Ko'rsatkichlar jadvalida shu toifadagi qator bo'lsa, xonalar jadvalidagi alohida xonalar takror
   sanalmasligi uchun sukut bo'yicha hisobga kiritilmaydi (xodim kirita oladi).
9. Sanoat 3 m ko'rsatkichi: har bino uchun qurilish maydoni × 3 m × qavatlar soni, undan ortig'i
   "3 m dan yuqori qism". Avtomatik ayirilmaydi.
10. Bir necha bino bo'lsa Hyu taklifi berilmaydi (ruxsat etilgan qavat hamma binodan baland bo'lsa 0).
11. Xodim ismi autentifikatsiyasiz, cookie orqali olinadi (tizim faqat SSH tunnel ortida).

## 8. Tekshirilmagan narsalar va cheklovlar

- **Ha, Ht, Hu, Hyu aniqligi o'lchanmagan** — etalon yo'q. Xona toifalash lug'ati va balandlik
  tanlash mantiq testlaridan o'tgan, lekin haqiqiy hujjatdagi to'g'riligi tasdiqlanmagan.
- **Faqat 5 obyekt.** AR albomi bitta (284459). Boshqa loyihachilar albomida jadval tuzilishi
  boshqacha bo'lishi mumkin.
- **Bir marta o'qish.** Yakuniy raqamlar bitta to'liq o'qishdan; oraliq o'qishlarda natija
  ±2–3 qatorga o'zgargan (model tasodifiyligi). "Jim xato 0" beshta hujjatda bir o'lchov natijasi,
  kafolat emas.
- **Ro'yxat maydonlarida jim xato o'lchanmaydi.** Hudud ko'rsatkichlari, balandliklar, sath
  maydonlari va xonalar bo'yicha etalon qiymati tizim ro'yxatida bor-yo'qligi tekshirilgan; tizim
  ortiqcha yoki noto'g'ri yorliqli element qo'shgan bo'lsa, bu o'lchovda ko'rinmaydi.
- **Kirill o'zbek hujjati** namunalarda yo'q edi (o'zbek lotin, ş/ç harfli lotin va rus tili bor).
- **OCR yo'q.** Ko'rsatkichlar rasm ichida bo'lsa o'qilmaydi; sahifada katta rastr bo'lsa
  ogohlantirish chiqadi (302030 loyihasining 3–7-sahifalari 91% rasm).
- **2 kartali server**da (TASK.md dagi asl reja) sinalmagan.
- **Uzoq muddatli barqarorlik** sinalmagan: "!!!!" nosozligi ≈4 soatlik yukda kuzatilmadi.
- **Brauzerda** faqat headless Chromium bilan ko'rilgan (skrinshotlar); chop etish ko'rinishi
  ko'z bilan tekshirilmagan.
- README'dagi 25 holatdan 4 tasi ko'rsatilmadi, 1 tasi qisman (3.5-bo'lim).

## 9. Tavsiyalar

1. Ikki modelni birga ishlating: har biri alohida ham nol jim xato berdi, lekin ular bir-birining
   tasodifiy xatosini ko'rsatadi (oraliq o'lchovlarda shunday bo'lgan).
2. Xodim har doim tekshirish ekranida manba parchasini ko'rib tasdiqlasin — tizim shunga
   mo'ljallangan, "yakunlash" barcha hajmlar tasdiqlanmaguncha o'chiq.
3. Ha/Ht/Hu uchun 10–20 ta obyekt bo'yicha qo'lda etalon tuzib, shu o'lchovni takrorlash kerak.
4. Qarorning rasmiy matni bilan koeffitsiyent variantlari matnini solishtirish kerak.
