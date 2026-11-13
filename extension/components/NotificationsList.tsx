import { api, formatTime, type Notification } from '@/lib/api';

interface Props {
  notifications: Notification[];
  onChanged: () => void;
  onError: (message: string) => void;
}

const LEVEL_LABELS: Record<Notification['level'], string> = {
  info: 'Info',
  success: 'Done',
  warning: 'Warning',
  error: 'Error',
  action_required: 'Action needed',
};

export default function NotificationsList({ notifications, onChanged, onError }: Props) {
  if (notifications.length === 0) {
    return <p className="empty">No notifications.</p>;
  }

  async function markRead(id: string) {
    try {
      await api.markRead(id);
      onChanged();
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error));
    }
  }

  return (
    <>
      {notifications.map((item) => (
        <article key={item.id} className={`card ${item.read ? 'read' : ''}`}>
          <span className={`badge ${item.level}`}>{LEVEL_LABELS[item.level]}</span>
          <h2>{item.title}</h2>
          {item.body && <p className="summary">{item.body}</p>}
          <p className="meta">
            {item.feature} · {formatTime(item.created_at)}
            {!item.read && (
              <button className="link" onClick={() => markRead(item.id)}>
                Mark read
              </button>
            )}
          </p>
        </article>
      ))}
    </>
  );
}
