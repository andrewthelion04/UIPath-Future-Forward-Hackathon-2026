import { formatTime, type Incident, type IncidentStatus, type Property } from '@/lib/api';

// Doar citire: deciziile hostului se iau din tab-ul Approvals.

const STATUS: Record<IncidentStatus, { label: string; tone: string }> = {
  awaiting_staff: { label: 'Asking maintenance', tone: 'info' },
  awaiting_decision: { label: 'Waiting for your approval', tone: 'action_required' },
  awaiting_guest: { label: 'Waiting for the guest', tone: 'info' },
  remediation_scheduled: { label: 'Repair in progress', tone: 'warning' },
  relocated: { label: 'Guest relocated, repair in progress', tone: 'warning' },
  resolved: { label: 'Resolved', tone: 'success' },
  dismissed: { label: 'Dismissed', tone: 'info' },
};

const GUEST_ANSWER: Record<string, string> = {
  accept: 'Guest accepted the move',
  stay: 'Guest preferred to stay',
  timeout: 'Guest did not answer (stays)',
};

export function isOpen(incident: Incident): boolean {
  return incident.status !== 'resolved' && incident.status !== 'dismissed';
}

function estimate(incident: Incident): string | null {
  if (!incident.eta_source) return null;
  if (incident.eta_hours == null) return 'Fix estimate: unknown';
  const hours = incident.eta_hours;
  return `Fix estimate: ${hours < 1 ? `${Math.round(hours * 60)} min` : `${hours} h`}`;
}

export default function IncidentsList({
  incidents,
  properties,
}: {
  incidents: Incident[];
  properties: Property[];
}) {
  if (incidents.length === 0) {
    return <p className="empty">No incidents. Staff and guest reports appear here.</p>;
  }
  const names = new Map(properties.map((property) => [property.id, property.name]));
  return (
    <>
      {incidents.map((incident) => {
        const status = STATUS[incident.status] ?? { label: incident.status, tone: 'info' };
        const facts = [
          estimate(incident),
          incident.recommendation_reason &&
            `Recommendation: ${incident.recommendation === 'relocate' ? 'offer relocation' : 'fix in place'}. ${incident.recommendation_reason}`,
          incident.guest_answer && GUEST_ANSWER[incident.guest_answer],
          incident.resolution,
        ].filter(Boolean);
        return (
          <article key={incident.id} className={isOpen(incident) ? 'card' : 'card read'}>
            <span className={`badge ${status.tone}`}>{status.label}</span>
            <h2>{names.get(incident.property_id) ?? incident.property_id}</h2>
            <p className="summary">{incident.description}</p>
            <p className="meta">
              {incident.reporter || 'unknown'} ({incident.reported_via}) · severity {incident.severity}
              {incident.report_count > 1 && ` · ${incident.report_count} reports`} · updated{' '}
              {formatTime(incident.updated_at)}
            </p>
            {facts.length > 0 && <p className="summary">{facts.join('\n')}</p>}
            {incident.tasks.length > 0 && (
              <ul className="tasks">
                {incident.tasks.map((task) => (
                  <li key={task.id}>
                    {task.status === 'done' ? '✓' : '•'} {task.id} {task.description} ·{' '}
                    {task.staff_name ?? 'unassigned'}
                    {task.completion_note && <span className="note">{task.completion_note}</span>}
                  </li>
                ))}
              </ul>
            )}
          </article>
        );
      })}
    </>
  );
}
