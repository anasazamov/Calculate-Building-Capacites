# Vazifa: infratuzilma yig'imi uchun hajmlarni hujjatdan o'qish — sinov tizimi

## 1. Kontekst
Men Samarqand Invest kompaniyasida ishlayman. Vazirlar Mahkamasining 151-son qarori
(08.04.2026), 4-ilovasiga ko'ra qurilish obyektlaridan infratuzilma yig'imi undiriladi.
Yig'im bino hajmlariga (m³) bog'liq, hajmlar esa ikki hujjatdan olinadi: loyiha (asosan
AR bo'limi albomi, PDF) va ekspertiza xulosasi (PDF). Hujjatlar o'zbek (lotin va kirill)
va rus tillarida, ko'pincha aralash.

Sinovning maqsadi bitta savolga javob berish: shu serverdagi ochiq modellar hajmlarni
hujjatdan ishonchli o'qiy oladimi. Natija ikki qism: (a) o'zing o'tkazgan sinov va uning
hisoboti, (b) xodim hujjat yuklab, o'qilgan hajmlarni tekshirib, yig'imni hisoblaydigan
veb-interfeys.

Server 8 soatga ijaraga olingan, shuning uchun 10-bo'limdagi tartibga amal qil. Savol
tug'ilsa va usiz davom etib bo'lsa, eng oqilona taxminni tanla, HISOBOT.md ga yoz va
davom et. Mendan faqat ishni to'xtatadigan narsada so'ra.

## 2. O'zgarmas qoidalar
1. Model koeffitsiyent tanlamaydi va pul hisoblamaydi. Uning ishi hujjatdan hajmga oid
   qiymatlarni topish va xonalarni toifalash. Kt, Ko, Kz, Kj, Kengash koeffitsiyenti va
   BHMni xodim interfeysda o'zi tanlaydi; hisobni Decimal'da yozilgan kod bajaradi.
   Sababi: koeffitsiyent huquqiy qaror, model uni asoslab bera olmaydi.
2. Har qiymat manbasi bilan saqlanadi: fayl, sahifa, hujjatdagi aynan matn parchasi.
   Parcha o'sha sahifa matnida so'zma-so'z topilmasa yoki raqam undan ajralmasa, qiymat
   qabul qilinmaydi. Sababi: model raqamni o'ylab topishi mumkin, bu tekshiruv ushlaydi.
3. Model ikki hujjatni bitta so'rovda birga ko'rmaydi. Loyiha va xulosa alohida o'qiladi,
   solishtirishni kod bajaradi. Sababi: birga ko'rsa, raqamlarni moslashtirib yuborishi mumkin.
4. Hujjat mazmuni serverdan tashqariga chiqmaydi: tashqi API'ga so'rov yo'q, telemetriya
   yo'q. Internet faqat model va paket yuklash uchun. Veb-server faqat 127.0.0.1 da
   tinglaydi (men SSH tunnel orqali kiraman). Faqat ~/samples/ dagi hujjatlarni och;
   interfeys orqali yuklanganlarini o'zing ochma. Terminalga hujjatning to'liq matnini
   chiqarma, diagnostika uchun qisqa parcha yetadi.
5. Tizim jim taxmin qilmaydi. Qiymat topilmasa "topilmadi", tekshiruvdan o'tmasa
   "tekshirilmadi" bo'ladi; model javobi buzuq bo'lsa ish to'liq xato bilan to'xtaydi.
   Standart qiymat qo'yib yuborish yo'q.

## 3. Hujjatlar haqida ma'lum narsalar
- Ekspertiza xulosasi (ShaffofQurilish, dompdf): matn qatlami toza. Binolar jadvali
  ustunlari: turi, nomi, qavati, qurilish maydoni, umumiy maydoni, hajmi. "Jami" qatori
  butun songa kesilgan qiymatlar yig'indisi bo'lishi mumkin (124433.19 o'rniga 124432);
  buni xato emas, "yaxlitlash farqi" deb ko'rsat. Hajm birligi ba'zan "m2" deb xato
  yozilgan; birlikni kontekstdan aniqla va belgilab qo'y.
- AR albomi: vektor PDF, varaqlar A3–A1. Ayrim sahifalarning matn qatlami 100 ming
  belgigacha, shuning uchun butun sahifani modelga berma: PyMuPDF bloklari va
  koordinatalari bilan kerakli jadval yoki shtampni ajrat. "Общие данные" varag'ida
  texnik-iqtisodiy ko'rsatkichlar; "Ведомость отделки помещений" va "Экспликация полов"
  varaqlarida xonalar va maydonlar; kesim varag'ida sath belgilari bor.
- Ayrim hujjatlarda ko'rsatkichlar past aniqlikdagi rasm ichida. Bunday sahifa "matnsiz"
  deb belgilanadi; OCR bu sinovga kirmaydi.

## 4. Server va modellar
- Kartalar: 2× NVIDIA RTX PRO 5000. Avval nvidia-smi bilan xotirani, bo'sh diskni
  (ikki model uchun 120 GB atrofida) va internet tezligini tekshir. Blackwell avlodi
  bo'lsa CUDA 12.8+ va PyTorch 2.7+ kerak; vLLM'ning yangi image'larida bu bor.
- 1-model (GPU 0): Qwen/Qwen3.8-27B-FP8, kamida 38 GB VRAM. Docker bo'lsa
  vllm/vllm-openai:qwen38 image'i, bo'lmasa vLLM 0.27 atrofidagi versiya.
  Bayroqlar: --reasoning-parser qwen3 --max-model-len 32768 --max-num-seqs 8
  --gpu-memory-utilization 0.90. Speculative decoding yoqilmaydi. Port 127.0.0.1:8001.
- 2-model (GPU 1): google/gemma-4-31B-it, ikkinchi mustaqil o'quvchi. Tayyor FP8 yoki
  sifatli 4-bit yig'masi bo'lsa o'shani ol (yuklash ham tezroq); bo'lmasa
  --quantization fp8 bilan sinab ko'r. Kartaga sig'masa yoki token kerak bo'lsa, menga
  ayt va 2-modelsiz davom et. Uni faqat 1-model bilan to'liq zanjir ishlagach qo'sh.
- Bu nom va bayroqlar 2026-yil noyabr holati. Mos kelmasa `vllm serve --help` va model
  kartasidan tekshir.
- So'rov sozlamalari: thinking o'chiq (chat_template_kwargs: {"enable_thinking": false}),
  javob JSON sxema bilan cheklangan, max_tokens qat'iy, temperature 0.2, top_p 0.8,
  top_k 20, presence_penalty 0. Presence penalty nol, chunki u takrorlanadigan raqam va
  kalitlarni jazolaydi. Takrorlanish halqasi yoki buzuq javob ko'rsang, model
  kartasidagi thinking'siz sozlamalarni (0.7 / 0.8 / 20) sinab, farqni hisobotga yoz.
- Ma'lum nosozliklar (Qwen3.6-27B'da vLLM bilan kuzatilgan; 3.8 shunga o'xshash tuzilgan):
  - Javob faqat "!!!!" bo'lib qoladi (0-token), keyingi barcha so'rovlarga o'tadi va
    server o'zi tiklanmaydi. Nazoratchi yoz: har javobda ketma-ket bir xil tokenni
    tekshir, davriy kanareyka so'rovi yubor; alomat chiqsa serverni qayta ishga tushir
    va hodisani jurnalga yoz.
  - Thinking yoqiq bo'lsa token chegarasi mulohaza ichida tugab, content bo'sh keladi.
  - Parallel yukda bir xil so'rovga javob o'zgarishi mumkin. Shuning uchun har model
    javobi xom holda saqlanadi va qayta so'ralmaydi.

## 5. Ishlov berish zanjiri
1. Qabul qilish: PyMuPDF bilan har sahifadan matn qatlami (koordinatalari bilan) va
   PNG rasm.
2. Varaq turi: shtamp va sarlavha bo'yicha (umumiy ma'lumotlar, bosh reja, qavat rejasi,
   kesim, eksplikatsiya, pardozlash jadvali, boshqa). Avval qoidalar, noaniq bo'lsa model.
3. O'qish, har hujjat alohida:
   - Xulosadan, har bino uchun: nomi va turi, qavatlar soni, qurilish maydoni, umumiy
     maydon, qurilish hajmi, tarx o'lchami, qavat balandliklari, xonadonlar soni; yana
     manzil, buyurtmachi, xulosa raqami va sanasi, qurilish turi.
   - Loyihadan: qurilish maydoni; qurilish hajmi (jami, yer usti, yer osti); sathlar
     bo'yicha maydonlar va belgilar; qavat balandliklari; xonalar yoki xona guruhlari
     (nomi, sathi, maydoni); xonadonlar va qavatlar soni; tarx o'lchami; manzil.
   - Har qiymat: {qiymat, birlik, sahifa, manba_matn}.
4. Toifalash: xona nomi → avtoturargoh (Ha), texnik (Ht), umumiy foydalanish (Hu),
   boshqa. Avval koddagi lug'at (паркинг, автостоянка, avtoturargoh; насосная,
   электрощитовая, венткамера, тех. помещение, texnik xona; лестничная клетка,
   лифтовой холл, приквартирная площадка, zinapoya, lift xolli, koridor va h.k.).
   Lug'atda yo'q nomni model taklif qiladi va u "xodim tasdiqlashi kerak" bo'ladi.
5. Hajmlar (kod):
   - Hb: loyihadagi jami qurilish hajmi, xulosa bilan solishtiriladi.
   - Ha, Ht, Hu: hujjatda hajm to'g'ridan-to'g'ri yozilgan bo'lsa o'sha; faqat maydon
     bo'lsa maydon × shu sath balandligi. Bu "hisoblangan" deb belgilanadi, formulasi
     ko'rinadi, xodim balandlikni yoki natijani sababini yozib o'zgartira oladi.
   - Hyu: ruxsat etilgan qavatlar sonini xodim kiritadi; kod undan yuqori qavatlar
     hajmini taklif qiladi.
6. Tekshiruvlar (kod): manba matni so'zma-so'z topilishi; yig'indilar (sathlar maydoni =
   umumiy maydon; yer usti + yer osti = jami hajm); loyiha va xulosa tengligi
   (yaxlitlashdan boshqa har qanday tafovut — farq); hajm qurilish maydoni × balandliklar
   yig'indisidan kichik emasligi; Ha + Ht + Hu ≤ Hb; 2-model bo'lsa ikki javob tengligi.

## 6. Hisob yadrosi (faqat kod, Decimal)
Ti = BHM × ((Hb + Hyu) − (Ha + Ht + Hu)) × Kt × Ko × Kz × Kj × Kkengash

- Kt: yangi qurilish 1; renovatsiya 0,5; rekonstruksiya 0,85.
- Ko: ijtimoiy xususiy obyekt 0,8; davlat ulushi 50%+ loyiha obyekti 0,5; sanoat binosi
  0,5; boshqa 1.
- Kz (aholi soni): 1 mln dan ortiq — 1 dan 2 gacha, qiymatni xodim kiritadi;
  500 ming–1 mln — 0,5; 250–500 ming — 0,2; 100–250 ming — 0,1; 100 minggacha — 0,05.
- Kj: markaziy qatnov ko'chasi 1; boshqa hudud 0,6.
- Kkengash: 0,01–1,5; sukut bo'yicha 1.
- Koeffitsiyentlar ko'paytmasi 0,01 dan kichik chiqsa 0,01 olinadi va natijada ko'rsatiladi.
- BHM: xodim kiritadi; sukut bo'yicha 440 000 so'm (2026-yil 1-sentabrdan).
- Hisob hajmi manfiy chiqsa — xato. Yakuniy summa butun so'mgacha ROUND_HALF_UP;
  20% oldindan to'lov ham shunday yaxlitlanadi, qolgani ayirma.
- Sanoat binosi tanlansa, har qavatning 3 m dan yuqori qismi hajmini alohida ko'rsat;
  uni avtomatik ayirma, qarorni xodim beradi.

Mos kelishi shart bo'lgan nazorat misollari (pytest):

| BHM | Hb | Ha | Ht | Hu | Kt | Ko | Kz | Kj | Yig'im | 20% | 80% |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 440000 | 1000 | 0 | 0 | 0 | 1 | 1 | 0,5 | 1 | 220 000 000 | 44 000 000 | 176 000 000 |
| 412000 | 12000 | 800 | 300 | 0 | 1 | 1 | 0,5 | 1 | 2 245 400 000 | 449 080 000 | 1 796 320 000 |
| 440000 | 1000 | 0 | 0 | 0 | 0,5 | 0,5 | 0,05 | 0,6 | 4 400 000 | 880 000 | 3 520 000 |
| 440000 | 36036 | 0 | 168.32 | 3335.8 | 1 | 1 | 0,5 | 0,6 | 4 294 208 160 | 858 841 632 | 3 435 366 528 |

Uchinchi qatorda ko'paytma 0,0075 chiqadi va 0,01 chegarasi qo'llanadi.
Hb = 100, Ha = 200 bo'lsa — xato.

## 7. Avval o'zing sinab ko'r (interfeysdan oldin)
Sinov hujjatlari ~/samples/ da: <id>_loyiha.pdf va <id>_xulosa.pdf; bo'lsa etalon.csv
(id, bino, Hb, Hyu, Ha, Ht, Hu). Nomlar boshqacha bo'lsa moslash; papka bo'sh bo'lsa
mendan so'ra.

Agar ichida 284459-sonli xulosa va 16 qavatli Tip-2 turar joy albomi bo'lsa, kutilgan
qiymatlar:
- Xulosa: qavatlar 16; tarx 26.97 × 26.89 m; qurilish maydoni 600.3 m²; umumiy maydon
  9410.0 m²; hajm 36036.0 m³; xonadonlar 96.
- Loyiha ("Общие данные"): площадь застройки 600.3; строительный объём 36036
  (надземная часть 34619.3, подземная 1416.7); подвал −3.200 — 465.9; 1-этаж — 442.6;
  2–16 этажи — 7439.3; общая площадь (лестничная клетка, приквартирная площадка,
  лифтовой холл, электрощитовая, инвентарная) — 1026.4; тех. помещения +52.420 — 35.8;
  общая площадь здания 9410.0; квартир 96.
- Albomda manzil "г. Наманган, ул. Темир йул", xulosada esa Samarqand, Maysazor
  ko'chasi. Tizim buni farq deb ko'rsatishi kerak.

Sinov tartibi:
1. Hisob yadrosi testlari (model yuklanayotgan paytda).
2. Model tutun sinovi: kanareyka; 50 ta ketma-ket va 16 ta parallel qisqa JSON so'rov;
   buzuq va bo'sh javoblar soni, tok/s, "!!!!" alomati.
3. To'liq zanjir samples'da. Har maydon bo'yicha: to'g'ri / noto'g'ri / topilmadi /
   tekshiruvda ushlandi. Asosiy ko'rsatkich "jim xato": tizim qabul qilgan, aslida
   noto'g'ri qiymatlar soni. U nol bo'lishi kerak.
4. Natijani HISOBOT.md ga yoz va menga qisqa xulosa ber. Jim xato nol bo'lsa, ruxsat
   kutmay interfeysga o't; nol bo'lmasa to'xta va menga ko'rsat.

## 8. Veb-interfeys (o'zbek tilida, lotin yozuvida)
FastAPI + Jinja2 + HTMX yoki oddiy JS, SQLite, PyMuPDF. Node yig'ish zanjiri yo'q,
bitta buyruq bilan ishga tushadi, 127.0.0.1:8000. Faqat 1-model ishlayotgan bo'lsa ham
ishlashi kerak.

1. Yuklash: loyiha PDF(lar)i va xulosa PDF. Ish navbatga tushadi, bosqichlar holati
   ko'rinadi.
2. Tekshirish: chapda sahifa rasmi, manba parchasi ajratib ko'rsatilgan (PyMuPDF
   search_for to'rtburchaklari). O'ngda maydonlar jadvali: loyihadagi qiymat, xulosadagi
   qiymat, 1-model, 2-model, tekshiruv belgilari. Maydon bosilsa o'sha sahifa va parcha
   ochiladi. Har hajm (Hb, Hyu, Ha, Ht, Hu) uchun tarkibi (qaysi xonalar, maydon ×
   balandlik), "tasdiqlash" va "o'zgartirish" (sababi majburiy).
3. Hisob: koeffitsiyentlarni xodim ro'yxatdan tanlaydi (qaror matni bilan), BHM,
   Kkengash, ruxsat etilgan qavatlar soni. Hisob formulaga qo'yilgan sonlar bilan jonli
   ko'rinadi. Barcha hajmlar tasdiqlanmaguncha "yakunlash" tugmasi o'chiq.
4. Hisob varaqasi: chop etiladigan bitta sahifa va JSON. Ichida barcha qiymatlar
   manbasi bilan, tekshiruvlar natijasi, kim nimani o'zgartirgani, model va vLLM
   versiyasi, so'rov matni xeshi.
5. Ro'yxat: barcha yuklangan ishlar, holati, hajmlar va yig'im; CSV eksport.

Har tasdiq va o'zgartirish vaqti bilan audit jurnaliga yoziladi.

## 9. Kod qoidalari
- Python 3.11+, type hints; pul va hajm uchun faqat Decimal.
- try/except ishlatma: xato qayerda va nega chiqqani ko'rinishi kerak. Yagona istisno
  ish (job) chegarasi: u yerda xatoni to'liq holda (nomi, fayl va qator, xabar, qaysi
  amal) saqlab ko'rsatish mumkin, keyin o'sha ish to'xtaydi. Xatoni yutish yoki umumiy
  xabar bilan almashtirish yo'q.
- Modullar: ingest, classify, extract, verify, volumes, fee, web. Har biriga pytest.
- Har bosqich oxirida git commit.

## 10. Vaqt rejasi
- 0:00–0:30 muhit tekshiruvi; 1-model yuklashni darhol fon rejimida boshla, 2-modelni
  navbatga qo'y.
- 0:30–2:00 ingest, varaq turi, hisob yadrosi va testlari.
- 2:00–3:30 model ishga tushadi; o'qish so'rovlari; samples'da to'liq zanjir; HISOBOT.md.
- 3:30–6:00 veb-interfeys.
- 6:00–7:00 2-model va ikki model kelishuvi.
- 7:00–7:40 8–16 parallel so'rov bilan yuk sinovi, nazoratchi sinovi.
- 7:40–8:00 topshirish.

Orqada qolsang shu tartibda tashla: 2-model, chop etiladigan varaq bezagi, sanoat 3 m
ko'rsatkichi. Hisob yadrosi, manba tekshiruvi va tekshirish ekrani tashlanmaydi.

## 11. Topshirish
- HISOBOT.md: nima ishlaydi; samples bo'yicha maydonma-maydon aniqlik va jim xatolar;
  tezlik (tok/s, bitta juftlikka ketgan vaqt); uchragan nosozliklar; aniq versiyalar va
  ishga tushirish buyruqlari; qilingan taxminlar; tekshirilmagan narsalar. Tekshirilgan
  faktni taxmindan ajratib yoz.
- README.md: yangi serverda noldan ishga tushirish.
- Loyiha arxivi (modellarsiz va hujjatlarsiz): server o'chadi, uni yuklab olaman.