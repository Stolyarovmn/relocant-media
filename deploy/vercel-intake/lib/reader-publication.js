import { createHash } from "node:crypto";
import policy from "./reader-publication-policy.json" with { type: "json" };

export { policy as PUBLICATION_POLICY };
const digest = value => createHash("sha256").update(value).digest("hex");
export const TERMS_SHA256 = digest(policy.terms);
export const PUBLICATION_LABELS = Object.freeze(policy.labels);
const L = PUBLICATION_LABELS;
const controls = new Set(Object.values(L));
export const isPublicationControl = text => controls.has(text);

export function materialFingerprint(payload) {
  return digest(JSON.stringify({ text: payload.text ?? "", attachments:
    (payload.attachments ?? []).map(a => ({ kind: a.kind, file_id: a.file_id,
      file_unique_id: a.file_unique_id ?? null })) }));
}

export function publicationEnabled(form) {
  return form.publication_version === policy.version;
}

function notice(draft, mid, text, labels) {
  return { text, key: "publication:" + mid, reply_markup: {
    keyboard: [...labels.map(label => [{ text: label }]),
      [{ text: "Отменить №" + draft.reference }]],
    resize_keyboard: true, is_persistent: true, one_time_keyboard: false,
  } };
}

function prompt(draft, mid) {
  const r = draft.payload.reader_publication;
  if (r.stage === "ownership") return notice(draft, mid,
    "Чьи материалы вы прислали? Выбор относится ко всем сообщениям и вложениям этого обращения. " +
    "Если авторы разные, укажите это в тексте — права проверит редактор.", [L.own,L.authorized,L.source]);
  if (r.stage === "permission") return notice(draft, mid, policy.terms, [L.allow,L.private]);
  if (r.stage === "signature") return notice(draft, mid,
    "Как подписать материал? Имя из профиля, телефон и фотография профиля автоматически не публикуются. " +
    "Без подписи — без имени и ссылки в посте; закрытый контакт для ответа редакции сохраняется.",
    [L.anonymous, ...(r.available_username ? [L.username] : []), L.name]);
  if (r.stage === "name") return notice(draft, mid,
    "Напишите имя или псевдоним для подписи — до 40 знаков. Фамилия, почта и телефон не нужны. " +
    "Это сообщение станет только подписью, а не частью новости.", [L.anonymous,L.back]);
  if (r.stage === "credit_confirm") return notice(draft, mid,
    "В публикации будет показана подпись: " + r.signature.label + "\n\n" +
    "Разрешаете открыто показать именно эту подпись читателям канала «Еду в Белград»? " +
    "Для @username будет доступен переход в Telegram-профиль. Другие сведения профиля не добавляются.",
    [L.credit,L.back,L.anonymous]);
  if (r.stage === "materials") return notice(draft, mid,
    "Дополните или исправьте материалы. Затем нажмите «Отправить» — новое содержимое нужно подтвердить заново.",
    ["Отправить №" + draft.reference]);
  return notice(draft, mid,
    "Проверьте обращение №" + draft.reference + "\n\nСообщений: " +
    Object.keys(draft.payload.reader_parts ?? {}).length + ". Вложений: " + draft.payload.attachments.length + ".\n" +
    "Использование: " + (r.permission === "allow" ? "разрешена публикация в «Еду в Белград»" : "только для редакции") + ".\n" +
    "Подпись: " + (r.signature?.mode === "anonymous" ? "без подписи" : r.signature?.label ?? "не публикуется") + ".\n\n" +
    "Материал проверит редактор. Согласие не означает автоматическую публикацию.",
    ["Отправить №" + draft.reference, ...(r.permission === "allow" ? [L.editCredit] : []),L.editPermission,L.more]);
}

export function beginPublication(draft, mid, message) {
  draft.status = "collecting";
  const username = message.from?.username;
  draft.payload.reader_publication = { version: policy.version, stage: "ownership",
    material_sha256: materialFingerprint(draft.payload),
    available_username: typeof username === "string" && /^[A-Za-z0-9_]{5,32}$/u.test(username)
      ? "@" + username : null,
    scope: { channel_id: policy.channel_id, channel_url: policy.channel_url },
    terms: policy.terms, terms_sha256: TERMS_SHA256,
  };
  return { draft, notice: prompt(draft, mid) };
}

export function publicationChanged(draft, mid, message) {
  const r = draft.payload.reader_publication;
  if (!r || r.material_sha256 === materialFingerprint(draft.payload)) return null;
  return beginPublication(draft, mid, message);
}

export function planPublication(draft, message, edited, receivedAt) {
  const r = draft.payload.reader_publication;
  if (!r || draft.status !== "collecting" || edited) return null;
  const text = message.text ?? "";
  const mid = message.message_id;
  const evidence = () => ({ at: receivedAt, message_id: mid });
  const step = stage => { r.stage = stage; return { draft, notice: prompt(draft,mid) }; };
  if (text === "Отменить №" + draft.reference || text === "Сменить тему №" + draft.reference) return null;
  const submit = text === "Отправить №" + draft.reference;
  if (r.stage === "materials" && submit) return beginPublication(draft,mid,message);
  if (r.stage === "ownership" && [L.own,L.authorized,L.source].includes(text)) {
    r.ownership = text === L.own ? "own" : text === L.authorized ? "authorized" : "source";
    r.ownership_evidence = evidence();
    if (r.ownership === "source") {
      r.permission = "private";
      r.permission_evidence = evidence();
      r.signature = { mode: "anonymous", label: "", evidence: evidence() };
      return step("review");
    }
    return step("permission");
  }
  if (r.stage === "permission" && [L.allow,L.private].includes(text)) {
    r.permission = text === L.allow ? "allow" : "private";
    r.permission_evidence = evidence();
    if (r.permission === "private") {
      r.signature = { mode: "anonymous", label: "", evidence: evidence() };
      return step("review");
    }
    return step("signature");
  }
  if (["signature","name","credit_confirm"].includes(r.stage) && text === L.anonymous) {
    r.signature = { mode: "anonymous", label: "", evidence: evidence() };
    return step("review");
  }
  if (["name","credit_confirm"].includes(r.stage) && text === L.back) return step("signature");
  if (r.stage === "signature" && text === L.username && r.available_username) {
    r.signature = { mode: "username", label: r.available_username };
    return step("credit_confirm");
  }
  if (r.stage === "signature" && text === L.name) return step("name");
  if (r.stage === "name" && typeof message.text === "string" && !controls.has(text) &&
      !message.photo && !message.video && !message.document && !message.voice && !message.audio) {
    const name = text.trim();
    if (!name || [...name].length > 40 || !/^[\p{L}\p{N} ._'’\-]+$/u.test(name) ||
        /\d{5}/u.test(name) || name.startsWith("/")) {
      return { draft, notice: notice(draft, mid,
        "Для подписи нужно короткое имя или псевдоним, без ссылок, почты и телефона.", [L.anonymous,L.back]) };
    }
    r.signature = { mode: "name", label: name };
    return step("credit_confirm");
  }
  if (r.stage === "credit_confirm" && text === L.credit) {
    r.signature.evidence = evidence();
    r.signature.disclosure = { ...evidence(), channel_id: policy.channel_id, label: r.signature.label };
    return step("review");
  }
  if (r.stage === "review" && text === L.editCredit && r.permission === "allow") return step("signature");
  if (r.stage === "review" && text === L.editPermission) return step("ownership");
  if (r.stage === "review" && text === L.more) return step("materials");
  if (r.stage === "review" && submit) {
    if (r.material_sha256 !== materialFingerprint(draft.payload)) return beginPublication(draft,mid,message);
    r.confirmed = evidence();
    r.stage = "confirmed";
    return { draft, notice: null, ready: true };
  }
  if (submit || controls.has(text) || text.startsWith("/")) return { draft, notice: prompt(draft,mid) };
  return null;
}
