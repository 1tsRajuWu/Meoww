/** Questions: the anonymous box, on the one screen allowed to show who asked.
 *
 * Two readers of a question exist and this page is the second one. The channel
 * the question was relayed to sees `anon 4F2A`, the text and the time — the relay
 * body is built from a closed field list, so a name cannot reach it. The operator
 * sees all of that *plus* the asker, and that asymmetry is not a loophole: it is
 * the promise, and the reason this page exists at all is that answering from a
 * phone, from Discord, on a card that scrolled away, was the only way it worked
 * before.
 *
 * The reply box here sends through the same delivery the button on the owner's
 * DM uses (`cogs.qna.deliver_answer`), so this page adds no second set of rules
 * about what an answer does — it only adds somewhere to type one.
 */

import { useState } from "react";

import { api } from "../api";
import { ago } from "../lib/format";
import { useLive } from "../lib/useLive";
import type { QuestionRow, QuestionsPayload } from "../types";
import { Card, Empty, Failure, Pill, Skeleton, Unread, useToast } from "../ui";

const INTERVAL = 30_000;

export function Questions() {
  const live = useLive(() => api.questions(), INTERVAL);

  if (live.loading && !live.data) return <Skeleton rows={6} />;
  if (live.error && !live.data) return <Failure message={live.error} onRetry={live.refresh} />;

  const data: QuestionsPayload | null = live.data;
  if (!data) return null;

  const read = data.read.questions;
  const waiting = data.questions.length;

  return (
    <div className="grid">
      <div className="page__head">
        <div>
          <span className="eyebrow">Asked by nobody, answered by you</span>
          <h1>Questions</h1>
        </div>
        <span className="readstamp">
          {live.refreshing ? "reading…" : `read ${ago((live.readAt ?? 0) / 1000)}`}
        </span>
      </div>

      {/* An inbox that could not be read is not an empty one. Said before the
          list, because an empty list below it would be the claim this exists to
          refuse — and the list below is therefore *replaced* when this band is
          shown rather than merely preceded by it: "Nothing is waiting" under
          this is the same sentence in a smaller font. */}
      {!read.ok && <Unread title="The questions could not be read" said={read.because} />}

      {/* There is nowhere to relay to. `/qna ask` refuses by naming the setting,
          and this says the same sentence rather than leaving an operator to work
          out why nothing is arriving. */}
      {!data.relay.configured && (
        <Card span={6} index={0} tone="warn" title="The box is closed">
          <p className="card__body">
            <code>{data.relay.setting}</code> is not set on this deployment, so{" "}
            <code>/qna ask</code> refuses and names it instead of posting anywhere. Anything
            already in the list below can still be answered.
          </p>
        </Card>
      )}

      <Card
        span={6}
        index={1}
        // A heading that counts is a heading that can lie: "Nothing is waiting"
        // and "0 open" are the two ways this card can say *nobody asked*, and a
        // reading that was never taken is not that. So the shell of the card
        // changes with `read.ok` as well, and the count is drawn as the em dash
        // the Access page uses for the same reason rather than as a zeroed
        // number nobody measured.
        title={
          !read.ok
            ? "Unread"
            : waiting === 0
              ? "Nothing is waiting"
              : `${waiting} waiting`
        }
        hint={
          !read.ok
            ? "The list above says why. Nothing below is a count."
            : waiting === 0
              ? "Every question that has been asked has an answer."
              : "Newest first. Answers are DMed and never posted."
        }
        action={
          <div className="question__tally">
            <Pill tone={!read.ok ? "warn" : waiting ? "accent" : "neutral"}>
              {read.ok ? `${data.totals.open} open` : "— open"}
            </Pill>
            <Pill>{read.ok ? `${data.totals.answered} answered` : "— answered"}</Pill>
          </div>
        }
      >
        {!read.ok ? (
          <Empty
            title="Nobody can say"
            body="The database could not be opened on this host, so this is not an empty box — it is the absence of a list. The questions are on disk either way; reload in a moment."
          />
        ) : waiting === 0 ? (
          <Empty
            title="No question is waiting on you"
            body="Questions arrive here as soon as somebody asks one with /qna ask, and the same question is relayed to the channel with no name on it."
          />
        ) : (
          <ul className="questions">
            {data.questions.map((question) => (
              <Question
                key={question.id}
                question={question}
                maxAnswer={data.max_answer}
                onAnswered={live.refresh}
              />
            ))}
          </ul>
        )}
      </Card>

      <Card span={6} index={2} title="What this page is allowed to show">
        <p className="card__body">
          The channel sees the question with no name on it, and the asker&apos;s DM never
          learns who answered. This screen is the third reader, and it holds both halves —
          the public handle the channel saw and the name beside it — because somebody has
          to decide whether an answer is worth more than one DM.
        </p>
      </Card>
    </div>
  );
}

/** One question: who asked it, what it says, and somewhere to answer it. */
function Question({
  question,
  maxAnswer,
  onAnswered,
}: {
  question: QuestionRow;
  maxAnswer: number;
  onAnswered: () => void;
}) {
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  const send = async () => {
    setBusy(true);
    try {
      const result = await api.answerQuestion(question.id, draft);
      // `sent: false` is a working request with nothing to send — an empty box, a
      // question answered in another tab, a DM Discord refused. The server's own
      // sentence is what explains which, so it is what is shown.
      toast(result.said, result.sent ? "good" : "warn");
      if (result.sent) {
        setDraft("");
        onAnswered();
      }
    } catch (exc) {
      toast(exc instanceof Error ? exc.message : String(exc), "danger");
    } finally {
      setBusy(false);
    }
  };

  return (
    <li className="question">
      <header className="question__head">
        <div>
          <span className="question__handle">{question.handle}</span>
          <span className="question__who">
            <strong>{question.asker}</strong> <code>{question.asker_id}</code>
          </span>
        </div>
        {/* Two lines, and only the column needs saying: the when is the date and
            the line under it is the server it came from, both read at the same
            size and dimness the column states. This second line carried
            `question__where`, a name the sheet never ruled — an intent to set
            the two apart that was never written, so it is gone rather than
            guessed at. */}
        <span className="question__when">
          {question.asked}
          <span>{question.server}</span>
        </span>
      </header>

      <p className="question__ask">{question.question}</p>

      {/* Only shown when the relay did *not* take it: a question the channel never
          saw is a different finding from one that is merely unanswered, and the
          webhook's own reason is the only thing that can explain it. */}
      {!question.relayed && (
        <p className="question__relay">
          <Pill tone="warn">Not relayed</Pill>
          {question.relay_detail || "the webhook did not accept it"}
        </p>
      )}

      <form
        className="question__form"
        onSubmit={(event) => {
          // A form rather than a bare button so Enter in the box would submit —
          // but the box is multiline, where Enter is a newline, so the button is
          // the only thing that sends. Kept as a form for the label and the
          // keyboard's own submit affordance.
          event.preventDefault();
          void send();
        }}
      >
        {/* Named twice on purpose: the visible label is what a person reads, and
            `aria-labelledby` is what the accessibility tree reads — including on
            the many questions whose label is off-screen. The design-law test
            requires the second by name, which is the only reason it survives a
            refactor that would quietly drop it. */}
        <label id={`answer-label-${question.id}`} className="question__label" htmlFor={`answer-${question.id}`}>
          Your answer, DMed to {question.asker}
        </label>
        <textarea
          id={`answer-${question.id}`}
          aria-labelledby={`answer-label-${question.id}`}
          className="input question__answer"
          value={draft}
          maxLength={maxAnswer}
          rows={3}
          placeholder="Say what you actually mean to say — this is not posted anywhere."
          onChange={(event) => setDraft(event.target.value)}
        />
        <div className="question__actions">
          <button
            type="submit"
            className="btn btn--primary"
            disabled={busy || draft.trim().length === 0}
          >
            {busy ? "Sending…" : "Send the answer"}
          </button>
          <span className="question__count">
            {draft.trim().length}/{maxAnswer}
          </span>
        </div>
      </form>
    </li>
  );
}
