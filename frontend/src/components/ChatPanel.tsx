import { useEffect, useRef, useState } from "react";
import { Bot, Send, User } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Card, CardBody, CardHeader, CardTitle } from "./ui/card";
import { AgentThinking } from "./AgentThinking";
import { cn } from "../lib/utils";
import type { Step } from "../lib/api";

/** Chat quick-actions: the chip shows `label`, but `message` is what the reader actually
 *  "says" to the assistant — a proper, courteous request rather than a bare keyword. */
const QUICK_ACTIONS: { label: string; message: string }[] = [
  {
    label: "Beurteilung kürzen",
    message:
      "Könntest du die Beurteilung bitte etwas straffen und auf das klinisch Wesentliche " +
      "konzentrieren, ohne wichtige Aussagen zu verlieren?",
  },
  {
    label: "Auffällige Werte erläutern",
    message:
      "Könntest du mir bitte die auffälligen Messwerte kurz erläutern — was bedeuten sie " +
      "klinisch, und worauf weisen sie hin?",
  },
  {
    label: "Referenzbereiche und Quellen nennen",
    message:
      "Könntest du bitte für jede beurteilte Messung den verwendeten Referenzbereich und " +
      "die zugrunde liegende Leitlinienquelle angeben?",
  },
];

export interface Message {
  role: "user" | "assistant";
  content: string;
  steps?: Step[];
  streaming?: boolean;
  /** Wall-clock seconds for the whole turn. Measured in the UI, not summed from the step
   *  timings: the sum misses everything BETWEEN the steps, which is precisely where the
   *  waiting the reader complains about actually lives. */
  startedAt?: number;
  elapsed?: number;
}

interface Props {
  messages: Message[];
  onSend: (text: string) => void;
  pending: boolean;
  disabled: boolean;
}

export function ChatPanel({ messages, onSend, pending, disabled }: Props) {
  const [text, setText] = useState("");
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, pending]);

  const submit = () => {
    const t = text.trim();
    if (!t || disabled || pending) return;
    onSend(t);
    setText("");
  };

  return (
    <Card className="h-full">
      <CardHeader>
        <Bot className="h-4 w-4 text-slate-400" />
        <CardTitle>KI-Assistent</CardTitle>
      </CardHeader>
      <CardBody className="flex flex-col gap-3">
        <div className="flex-1 space-y-3 overflow-auto pr-1">
          {messages.length === 0 && (
            <p className="mt-4 text-center text-sm text-slate-400">
              Bitte den Assistenten, Formulierungen zu überarbeiten, die Beurteilung zu überdenken
              oder eine weitere Analyse durchzuführen.
            </p>
          )}
          {/* Speaker attribution: a small avatar beside each message, and the bubble
              corner NEAREST the avatar flattened — the "tail" points at whoever spoke. */}
          {messages.map((m, i) => (
            <div
              key={i}
              className={cn(
                "flex items-start gap-2",
                m.role === "user" ? "justify-end" : "justify-start"
              )}
            >
              {m.role === "assistant" ? (
                <>
                  <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-slate-200 text-slate-600">
                    <Bot className="h-4 w-4" />
                  </span>
                  <div className="max-w-[85%]">
                    {((m.steps && m.steps.length > 0) || m.streaming) && (
                      <AgentThinking steps={m.steps ?? []} live={m.streaming} elapsed={m.elapsed} />
                    )}
                    {m.content && (
                      <div className="rounded-2xl rounded-tl-sm bg-slate-100 px-3 py-2 text-sm text-slate-700">
                        <div className="chat-md">
                          <ReactMarkdown remarkPlugins={[remarkGfm]}>{m.content}</ReactMarkdown>
                        </div>
                      </div>
                    )}
                  </div>
                </>
              ) : (
                <>
                  <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-tr-sm bg-sky-600 px-3 py-2 text-sm text-white">
                    {m.content}
                  </div>
                  <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-sky-600 text-white">
                    <User className="h-4 w-4" />
                  </span>
                </>
              )}
            </div>
          ))}
          <div ref={endRef} />
        </div>

        {/* Quick actions: a short LABEL on the chip, but a proper, friendly request sent to
            the assistant — so the exchange reads like a real question, not a keyword.
            Hidden while nothing can be sent. */}
        {!disabled && !pending && (
          <div className="flex flex-wrap gap-1.5">
            {QUICK_ACTIONS.map((q) => (
              <button
                key={q.label}
                onClick={() => onSend(q.message)}
                title={q.message}
                className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-0.5 text-[11px] text-slate-500 hover:border-sky-300 hover:text-sky-700"
              >
                {q.label}
              </button>
            ))}
          </div>
        )}

        <div className="flex items-end gap-2 border-t border-slate-100 pt-3">
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
            rows={1}
            disabled={disabled}
            placeholder={
              disabled
                ? "Zuerst Bemaßungen vornehmen…"
                : "Frage zu Messwerten stellen oder Änderung am Befundentwurf anfordern…"
            }
            className="max-h-32 flex-1 resize-none rounded-lg border border-slate-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-sky-400/40 disabled:bg-slate-50"
          />
          <button
            onClick={submit}
            disabled={disabled || pending || !text.trim()}
            className="rounded-lg bg-sky-600 p-2 text-white transition-colors hover:bg-sky-700 disabled:opacity-40"
          >
            <Send className="h-4 w-4" />
          </button>
        </div>
      </CardBody>
    </Card>
  );
}
