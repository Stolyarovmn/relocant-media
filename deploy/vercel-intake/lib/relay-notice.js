import { isStartMessage } from "./intake.js";
import { isPublicationControl } from "./reader-publication.js";

const command = /^(Отправить|Сменить тему|Отменить) №(RM-[A-Z0-9]{4}-[A-Z0-9]{4}|[0-9]{1,18})$/u;

// Only acknowledges durable transport. Canonical actions keep their own writer.
export function relayNotice(state, actor, message, updateId, now) {
  const text = message.text ?? message.caption ?? "";
  if (typeof text !== "string") return null;
  const button = command.exec(text);
  const routes = [...Object.values(state.form.routes).map(([label]) => label), "Сотрудничество", "Реклама"];
  const current = state.cases.filter(c => c.telegram_user_id === actor.user &&
    c.payload.chat_id === actor.chat && (c.payload.topic_id ?? null) === actor.topic).at(-1);
  const known = button && [...(state.button_receipts ?? []), ...state.cases.map(c => ({
    user: c.telegram_user_id, chat: c.payload.chat_id, topic: c.payload.topic_id ?? null,
    reference: c.reference, status: c.status, expires_at: Infinity,
  }))].find(c => c.user === actor.user && c.chat === actor.chat && c.topic === actor.topic &&
    c.reference === button[2] && c.expires_at > now);
  const hasDraft = current && ["choosing", "collecting"].includes(current.status);
  const labels = !hasDraft
    ? { reply_markup: { keyboard: [[{ text: state.form.new_case }]], resize_keyboard: true,
      is_persistent: true, one_time_keyboard: false } } : {};
  if (isPublicationControl(text)) return {
    key: "button:" + updateId, ...labels, receipt_only: true,
    text: "Это кнопка прежнего шага. Используйте актуальное меню. Повторное обращение не создано.",
  };
  if (button) return {
    key: `button:${updateId}`, ...labels,
    ...(["cancelled", "new"].includes(known?.status) && { receipt_only: true }),
    text: known?.status === "cancelled" ? `Черновик №${known.reference} уже отменён.` :
      known?.status === "new" ? `Обращение №${known.reference} уже отправлено в работу.` :
        hasDraft ? "Это кнопка прежнего меню. Продолжите текущий черновик кнопками внизу чата." :
          "Это кнопка прежнего меню. Для нового обращения нажмите «Новое обращение». " +
          "Она не создаёт повторную заявку.",
  };
  // A double tap must not become a second case or silently disappear.
  if (routes.includes(text) && current?.status === "new") return {
    key: `button:${updateId}`, ...labels,
    receipt_only: true,
    text: `Обращение №${current.reference} уже отправлено в работу.`,
  };
  const content = typeof text === "string" && text.trim() ||
    ["photo", "video", "document", "voice", "audio"].some(k => message[k]);
  if (!content || text.startsWith("/")) return null;
  const reply = message.reply_to_message;
  const reference = reply?.from?.id === state.bot_id &&
    /(?:обращение|обращению|заявке|предложка) №(RM-[A-Z0-9]{4}-[A-Z0-9]{4}|[0-9]{1,18})/iu
      .exec(reply.text ?? "")?.[1];
  return {
    key: `received:${updateId}`,
    text: isStartMessage(state.form, message) ? state.form.menu :
      (reference ? `Дополнение к обращению №${reference} получено и сохранено. Передадим его в редакцию.` :
        "Сообщение и вложения получены и сохранены. Передадим их в редакцию. " +
        "Дополнение обработаем вместе с вашим обращением."),
  };
}
