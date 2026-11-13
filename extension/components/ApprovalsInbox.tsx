import { useState } from 'react';

import { api, formatTime, type Approval } from '@/lib/api';

interface Props {
  approvals: Approval[];
  onChanged: () => void;
  onError: (message: string) => void;
}

function ApprovalCard({ approval, onChanged, onError }: { approval: Approval } & Omit<Props, 'approvals'>) {
  const [option, setOption] = useState(approval.options[0]?.key);
  const [busy, setBusy] = useState(false);
  const single = approval.options.length === 1;

  async function decide(decision: 'approve' | 'reject') {
    setBusy(true);
    try {
      // Decizia e idempotentă pe server: un dublu clic nu execută acțiunea de două ori.
      await api.decide(approval.id, decision, decision === 'approve' ? option : undefined);
      onChanged();
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <article className="card">
      <h2>{approval.title}</h2>
      <p className="meta">
        {approval.feature} · {formatTime(approval.created_at)}
      </p>
      <p className="summary">{approval.summary}</p>
      {!single && (
        <fieldset disabled={busy}>
          {approval.options.map((item) => (
            <label key={item.key} className="option">
              <input
                type="radio"
                name={`option-${approval.id}`}
                checked={option === item.key}
                onChange={() => setOption(item.key)}
              />
              <span>
                <strong>{item.label}</strong>
                {item.description && <small>{item.description}</small>}
              </span>
            </label>
          ))}
        </fieldset>
      )}
      <div className="actions">
        <button className="primary" disabled={busy} onClick={() => decide('approve')}>
          {single ? approval.options[0]?.label ?? 'Approve' : 'Approve'}
        </button>
        <button disabled={busy} onClick={() => decide('reject')}>
          Reject
        </button>
      </div>
    </article>
  );
}

export default function ApprovalsInbox({ approvals, onChanged, onError }: Props) {
  if (approvals.length === 0) {
    return <p className="empty">Nothing is waiting for your decision.</p>;
  }
  return (
    <>
      {approvals.map((approval) => (
        <ApprovalCard key={approval.id} approval={approval} onChanged={onChanged} onError={onError} />
      ))}
    </>
  );
}
