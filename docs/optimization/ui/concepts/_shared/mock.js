// Общие мок-данные для концепций редизайна Mini App (вымышленные люди и объявления).
// Подключение: <script src="../_shared/mock.js"></script> → window.MOCK
window.MOCK = {
  accounts: [
    { id: 1, name: "osman",  color: "#3b82f6" },
    { id: 2, name: "anna",   color: "#22c55e" },
    { id: 3, name: "dieter", color: "#ef4444" },
    { id: 4, name: "lena",   color: "#f59e0b" },
    { id: 5, name: "kai",    color: "#a855f7" },
  ],
  // Входящие: kind in = ждут нас, out = ждём клиента
  threads: [
    { id: "t1", acc: 1, buyer: "Markus W.", lang: "de", ad: "Sitzbank 3er VW T6 Multivan", price: 1200, floor: 950, offer: 900,
      kind: "in", draft: true, unread: true, ago: "3 мин", time: "12:41",
      last_de: "Hallo, ist die Sitzbank noch da? Würden Sie 900 € nehmen? Ich könnte Samstag abholen.",
      last_ru: "Здравствуйте, сиденье ещё есть? Отдадите за 900 €? Могу забрать в субботу.",
      tags: ["торгуется", "самовывоз"], score: 82 },
    { id: "t2", acc: 2, buyer: "Agnieszka K.", lang: "pl", ad: "Drehkonsole Fahrersitz Sprinter", price: 380, floor: 320, offer: null,
      kind: "in", draft: true, unread: true, ago: "18 мин", time: "12:26",
      last_de: "Dzień dobry, czy wysyłka do Polski jest możliwa?",
      last_ru: "Добрый день, возможна ли доставка в Польшу?",
      tags: ["доставка"], score: 64 },
    { id: "t3", acc: 3, buyer: "Thomas B.", lang: "de", ad: "Rücksitzbank Opel Zafira Life", price: 650, floor: 550, offer: 600,
      kind: "in", draft: false, unread: false, ago: "1 ч", time: "11:40",
      last_de: "Letztes Angebot: 600 € und ich hole heute noch ab.",
      last_ru: "Последнее предложение: 600 € и заберу ещё сегодня.",
      tags: ["готов купить"], score: 91 },
    { id: "t4", acc: 1, buyer: "Jan de V.", lang: "nl", ad: "Tisch Schienensystem T5", price: 220, floor: 180, offer: null,
      kind: "out", draft: false, unread: false, ago: "2 ч", time: "10:52",
      last_de: "Danke, ich melde mich morgen.", last_ru: "Спасибо, напишу завтра.", tags: ["ждём"], score: 48 },
    { id: "t5", acc: 4, buyer: "Sabine M.", lang: "de", ad: "Einzelsitz drehbar Ducato", price: 290, floor: 250, offer: 250,
      kind: "out", draft: false, unread: false, ago: "5 ч", time: "07:30",
      last_de: "Ok, 270 € passt. Wann kann ich kommen?", last_ru: "Ок, 270 € подходит. Когда можно приехать?", tags: ["договорились"], score: 88 },
    { id: "t6", acc: 5, buyer: "Mehmet Y.", lang: "tr", ad: "Sitzbank Vito 2+1", price: 800, floor: 700, offer: 500,
      kind: "in", draft: true, unread: false, ago: "вчера", time: "21:14",
      last_de: "500 € bar sofort, letzte Preis?", last_ru: "500 € наличными сразу, последняя цена?", tags: ["низкая цена"], score: 35 },
    { id: "t7", acc: 2, buyer: "Peter H.", lang: "de", ad: "Isofix Sitz T6.1", price: 340, floor: 300, offer: null,
      kind: "out", draft: false, unread: false, ago: "вчера", time: "18:02",
      last_de: "Ist der Sitz unfallfrei?", last_ru: "Сиденье без аварий?", tags: [], score: 55 },
  ],
  // Открытый тред t1: лента + черновик ИИ (3 этапа отправки)
  thread: {
    id: "t1",
    events: [
      { who: "in",  time: "Вт 18:20", de: "Guten Tag, ist die Sitzbank für einen T6 Multivan Comfortline passend?", ru: "Добрый день, подойдёт ли сиденье для T6 Multivan Comfortline?" },
      { who: "out", time: "Вт 18:24", de: "Hallo Markus, ja, sie passt auf alle T6/T6.1 Multivan mit Schienensystem. Viele Grüße", ru: "Здравствуйте, Маркус, да, подходит ко всем T6/T6.1 Multivan с рельсами. С уважением", auto: true },
      { who: "in",  time: "Ср 09:05", de: "Gibt es Gebrauchsspuren? Können Sie Fotos von der Unterseite schicken?", ru: "Есть следы использования? Пришлёте фото снизу?" },
      { who: "out", time: "Ср 09:40", de: "Kaum Gebrauchsspuren, Fotos habe ich angehängt. Viele Grüße", ru: "Почти без следов, фото приложил. С уважением" },
      { who: "in",  time: "12:41", de: "Hallo, ist die Sitzbank noch da? Würden Sie 900 € nehmen? Ich könnte Samstag abholen.", ru: "Здравствуйте, сиденье ещё есть? Отдадите за 900 €? Могу забрать в субботу." },
    ],
    draft: {
      lang: "de",
      text: "Hallo Markus, ja, die Sitzbank ist noch da. 900 € ist mir etwas zu wenig – für 1.050 € gehört sie Ihnen, Abholung am Samstag passt gut. Viele Grüße",
      ru: "Здравствуйте, Маркус, да, сиденье ещё есть. 900 € для меня маловато — за 1 050 € оно ваше, самовывоз в субботу подходит. С уважением",
      suggestedPrice: 1050,
    },
    deal: { price: 1200, floor: 950, offer: 900, ourLast: 1100, summary: "Клиент серьёзный, сравнивает с другим объявлением, готов забрать в субботу." },
  },
  // Карточка клиента
  client: {
    name: "Markus W.", email: "markus.w@example.invalid", lang: "de", since: "март 2026",
    stats: { inquiries: 3, bought: 1, spent: 480, avgReply: "12 мин" },
    note: "Вежливый, платит наличными, забирает сам. Торгуется ~10 %.",
    history: [
      { ad: "Sitzbank 3er VW T6 Multivan", status: "идёт торг", price: 1200, date: "сегодня" },
      { ad: "Tischplatte Multivan", status: "куплено", price: 480, date: "14.07" },
      { ad: "Kopfstützen T6 (2 Stk)", status: "не договорились", price: 90, date: "02.05" },
    ],
  },
  // Обзор продаж
  sales: {
    period: "Октябрь 2026",
    total: 7480, count: 19, avgDiscount: 8.4, replyMedian: "9 мин",
    byDay: [320, 0, 610, 950, 0, 280, 1200, 0, 450, 380, 0, 990, 650, 0, 0, 720, 300, 0, 430, 0],
    byAccount: [ { acc: 1, sum: 2650 }, { acc: 2, sum: 1480 }, { acc: 3, sum: 1210 }, { acc: 4, sum: 1320 }, { acc: 5, sum: 820 } ],
    recent: [
      { ad: "Rücksitzbank Zafira Life", price: 600, acc: 3, date: "сегодня" },
      { ad: "Einzelsitz Ducato", price: 270, acc: 4, date: "вчера" },
      { ad: "Tischplatte Multivan", price: 480, acc: 1, date: "14.10" },
    ],
  },
};
