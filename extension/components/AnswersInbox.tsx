import { useState } from 'react';
import { api, type HostAnswer } from '@/lib/api';

function AnswerCard({ answer, onChanged, onError }: {
  answer: HostAnswer; onChanged: () => void; onError: (message: string) => void;
}) {
  const [text, setText] = useState(answer.reply ?? '');
  const [busy, setBusy] = useState(false);
  const uncertain = answer.status === 'sending';
  async function send() {
    setBusy(true);
    try { await api.reply(answer.id, text); onChanged(); }
    catch (error) { onError(error instanceof Error ? error.message : String(error)); }
    finally { setBusy(false); }
  }
  async function check(delivered: boolean) {
    setBusy(true);
    try { await api.checkDelivery(answer.id, delivered); onChanged(); }
    catch (error) { onError(error instanceof Error ? error.message : String(error)); }
    finally { setBusy(false); }
  }
  return <article className="card">
    <h2>Reply to {answer.guest_name}</h2>
    <p className="meta">{answer.property_name}</p>
    <p className="summary">{answer.question}</p>
    {answer.context_note && <p className="meta">{answer.context_note}</p>}
    {uncertain && <div role="alert"><p>Check the PMS conversation, then confirm:</p>
      <button disabled={busy} onClick={() => void check(true)}>Message was delivered</button>
      <button disabled={busy} onClick={() => void check(false)}>Not delivered — allow retry</button>
    </div>}
    <label>Your reply<textarea value={text} maxLength={4000} disabled={busy || uncertain}
      onChange={e => setText(e.target.value)} style={{width:'100%', minHeight:100, boxSizing:'border-box'}} /></label>
    <p className="meta">Your exact message will be sent through the guest's PMS.</p>
    <button className="primary" disabled={busy || uncertain || !text.trim()} onClick={() => void send()}>Send reply to guest</button>
  </article>;
}

export default function AnswersInbox({ answers, onChanged, onError }: {
  answers: HostAnswer[]; onChanged: () => void; onError: (message: string) => void;
}) {
  return <>{answers.map(answer => <AnswerCard key={answer.id} answer={answer} onChanged={onChanged} onError={onError} />)}</>;
}
