const mediaTypes = ["photo", "video", "document", "voice"];
const greeting = /^(?:здравствуйте|здравствуй|привет|приветствую|доброе утро|добрый день|добрый вечер)[\s.!?,…👋]*$/iu;

export function readerIdentity(message) {
  const author = message.from;
  const chat = message.chat;
  if (!Number.isSafeInteger(author?.id) || author.id <= 0 || author.is_bot ||
      !Number.isSafeInteger(message.message_id) || message.message_id <= 0 ||
      !Number.isSafeInteger(chat?.id)) {
    return null;
  }
  if (chat.type === "private" && chat.id === author.id) {
    return { user: author.id, chat: chat.id, topic: null };
  }
  const topic = message.direct_messages_topic?.topic_id;
  if (chat.is_direct_messages === true && chat.id < 0 &&
      Number.isSafeInteger(topic) && topic > 0) {
    return { user: author.id, chat: chat.id, topic };
  }
  return null;
}

function getParts(message) {
  const attachments = [];
  for (const kind of mediaTypes) {
    const item = kind === "photo" ? message.photo?.at(-1) : message[kind];
    if (typeof item?.file_id === "string") {
      attachments.push({
        kind,
        file_id: item.file_id,
        file_unique_id: item.file_unique_id ?? null,
      });
    }
  }
  return { text: (message.text ?? message.caption ?? "").trim(), attachments };
}

function combineParts(parts) {
  const contents = Object.entries(parts).sort(([a], [b]) => Number(a) - Number(b));
  const attachments = new Map();
  const text = contents.map(([, part]) => part.text).filter(Boolean).join("\n");
  for (const [, part] of contents) {
    for (const file of part.attachments) {
      attachments.set(file.file_unique_id || file.file_id, file);
    }
  }
  return { text, attachments: [...attachments.values()] };
}

const rows = (labels) => labels.map((text) => ({ text })).reduce((keyboard, button, i) => {
  if (i % 2 === 0) keyboard.push([]);
  keyboard.at(-1).push(button);
  return keyboard;
}, []);

function reply(text, key, labels, permanent = true) {
  return {
    text,
    key,
    ...(labels && { reply_markup: {
      keyboard: rows(labels),
      resize_keyboard: true,
      one_time_keyboard: !permanent,
      ...(permanent && { is_persistent: true }),
    } }),
  };
}

function menu(form, draft, mid) {
  const labels = Object.values(form.routes).map(([label]) => label);
  labels.push(`Отменить №${draft.reference}`);
  return {
    ...reply(form.menu, draft.payload.reader_notices?.menu ? `menu:${mid}` : "menu", labels),
    submit_on_pick: true,
  };
}

function formPrompt(form, draft, mid) {
  const [label, instruction] = form.routes[draft.kind];
  const submit = `Отправить №${draft.reference}`;
  return reply(`${label}\n\n${instruction}\n\nКогда всё пришлёте, нажмите «${submit}».`,
    `prompt:${mid}`, [submit, `Сменить тему №${draft.reference}`,
      `Отменить №${draft.reference}`], false);
}

function sentMenu(draft) {
  return Object.entries(draft.payload.reader_notices ?? {})
    .filter(([name]) => name === "menu" || name.startsWith("menu:"))
    .map(([, notice]) => notice)
    .sort((a, b) => a.received_at.localeCompare(b.received_at) ||
      (a.telegram_message_id || 0) - (b.telegram_message_id || 0))
    .at(-1);
}

function accepts(draft, route) {
  return Boolean(draft.payload.text) ||
    (route === "news_tip" && draft.payload.attachments.length > 0);
}

export function planDraft(form, current, message, edited, allocateReference, actor, receivedAt) {
  const mid = message.message_id;
  const part = getParts(message);
  const start = !part.attachments.length && (
    part.text === form.new_case || /^\/start(?:@\w+)?(?:\s|$)/u.test(part.text) ||
    greeting.test(part.text)
  );
  const active = ["choosing", "collecting"].includes(current?.status);
  const known = { Сотрудничество: "partnership", Реклама: "ad_request" };
  for (const [kind, [label]] of Object.entries(form.routes)) known[label] = kind;
  const kind = !part.attachments.length && Object.hasOwn(known, part.text) ? known[part.text] : null;
  const command = !part.attachments.length &&
    /^(Отправить|Сменить тему|Отменить) №(RM-[A-Z0-9]{4}-[A-Z0-9]{4}|[0-9]{1,18})$/u.exec(part.text);
  const legacyCommand = /^\/(tip|correction|ad|cancel)(?:@\w+)?(?:\s|$)/u.test(part.text);
  const target = message.reply_to_message?.message_id;
  if (legacyCommand || (edited && !Object.hasOwn(current?.payload.reader_parts ?? {}, mid)) ||
      (!start && target && !Object.values(current?.payload.reader_notices ?? {})
        .some((item) => item.status === "sent" && item.telegram_message_id === target))) {
    return null;
  }
  if (current?.status === "new" && !start && (target || edited ||
      (part.attachments.length && Date.parse(receivedAt) - Date.parse(current.payload.accepted_at)
        <= 1_800_000))) {
    return null;
  }
  if (!part.text && !part.attachments.length && !edited) return null;
  if (!active && (command || (!start && kind && current?.status === "new"))) return null;

  const draft = active ? { ...current, payload: { ...current.payload } } : {
    reference: allocateReference(),
    kind: "reader_unclassified",
    status: "choosing",
    telegram_user_id: actor.user,
    telegram_message_id: mid,
    payload: {
      city: "belgrade",
      reader_intake: true,
      chat_id: actor.chat,
      received_at: receivedAt,
      text: "",
      attachments: [],
      ...(actor.topic !== null && {
        topic_id: actor.topic, channel_id: actor.channel, origin: "channel_direct_messages",
      }),
    },
  };
  draft.payload.public_reference ??= draft.reference;
  const unchanged = { draft, notice: null };
  if (command && !edited) {
    if (draft.reference !== command[2]) return unchanged;
    if (command[1] === "Отменить") {
      draft.status = "cancelled";
      return { draft, notice: reply(`Черновик №${draft.reference} отменён.`,
        "cancelled", [form.new_case]) };
    }
    if (command[1] === "Сменить тему" && draft.status === "collecting") {
      draft.status = "choosing";
      return { draft, notice: menu(form, draft, mid) };
    }
    if (command[1] !== "Отправить" || draft.status !== "collecting") return unchanged;
    if (!accepts(draft, draft.kind)) {
      return { draft, notice: reply("Добавьте описание или вложение, затем отправьте заявку.",
        `choice:${mid}`) };
    }
  } else if (active && kind && !edited && draft.status === "choosing") {
    const previousMenu = sentMenu(draft);
    if (previousMenu?.status !== "sent") return unchanged;
    if (!previousMenu.submit_on_pick) {
      draft.kind = kind;
      draft.status = "collecting";
      return { draft, notice: formPrompt(form, draft, mid) };
    }
    if (!accepts(draft, kind)) {
      return { draft, notice: reply(form.routes[kind][1] + "\n\nЗатем выберите действие ниже.",
        `choice:${mid}`) };
    }
    draft.kind = kind;
  } else {
    let notice = null;
    if (!active || start) {
      notice = draft.status === "collecting"
        ? formPrompt(form, draft, mid) : menu(form, draft, mid);
    }
    if (start || (!active && kind) || (kind && kind === draft.kind && !edited)) {
      return { draft, notice };
    }
    const parts = { ...draft.payload.reader_parts, [mid]: part };
    const combined = combineParts(parts);
    if (Object.keys(parts).length > 32 || [...combined.text].length > 20_000 ||
        combined.attachments.length > 24) {
      notice ??= reply("В обращении помещается до 32 сообщений, 24 вложений и 20 000 знаков.",
        `limit:${mid}`);
      return { draft, notice };
    }
    Object.assign(draft.payload, combined, {
      reader_parts: parts, media_present: combined.attachments.length > 0,
    });
    return { draft, notice };
  }
  draft.status = "new";
  draft.payload.accepted_at = receivedAt;
  const original = Object.keys(draft.payload.reader_parts ?? {}).map(Number);
  if (original.length) draft.telegram_message_id = Math.max(...original);
  const text = `Ваше обращение №${draft.reference} отправлено в работу.\n\n` +
    "Здесь сообщим результат. Фото, видео или уточнение можно дослать ответом на это сообщение.";
  return { draft, notice: reply(text, "accepted", [form.new_case]) };
}
