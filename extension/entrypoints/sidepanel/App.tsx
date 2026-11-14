import { useCallback, useEffect, useState } from 'react';

import ApprovalsInbox from '@/components/ApprovalsInbox';
import IncidentsList, { isOpen } from '@/components/IncidentsList';
import NotificationsList from '@/components/NotificationsList';
import PropertiesList from '@/components/PropertiesList';
import {
  api,
  API_URL,
  type Approval,
  type Incident,
  type Notification,
  type Property,
} from '@/lib/api';

type Tab = 'approvals' | 'notifications' | 'incidents' | 'properties';

const REFRESH_MS = 5000;

export default function App() {
  const [tab, setTab] = useState<Tab>('approvals');
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [notifications, setNotifications] = useState<Notification[]>([]);
  const [properties, setProperties] = useState<Property[]>([]);
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [nextApprovals, nextNotifications, nextProperties, nextIncidents] = await Promise.all([
        api.approvals(),
        api.notifications(),
        api.properties(),
        api.incidents(),
      ]);
      setApprovals(nextApprovals);
      setNotifications(nextNotifications);
      setProperties(nextProperties);
      setIncidents(nextIncidents);
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), REFRESH_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  const unread = notifications.filter((item) => !item.read).length;
  const tabs: { id: Tab; label: string; count: number }[] = [
    { id: 'approvals', label: 'Approvals', count: approvals.length },
    { id: 'notifications', label: 'Notifications', count: unread },
    { id: 'incidents', label: 'Incidents', count: incidents.filter(isOpen).length },
    { id: 'properties', label: 'Properties', count: 0 },
  ];

  return (
    <main>
      <header>
        <h1>StayPilot AI</h1>
        <button className="link" onClick={() => void refresh()}>
          Refresh
        </button>
      </header>
      <nav>
        {tabs.map((item) => (
          <button
            key={item.id}
            className={tab === item.id ? 'tab active' : 'tab'}
            onClick={() => setTab(item.id)}
          >
            {item.label}
            {item.count > 0 && <span className="count">{item.count}</span>}
          </button>
        ))}
      </nav>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {!loaded ? (
        <p className="empty">Loading from {API_URL}...</p>
      ) : (
        <section>
          {tab === 'approvals' && (
            <ApprovalsInbox approvals={approvals} onChanged={() => void refresh()} onError={setError} />
          )}
          {tab === 'notifications' && (
            <NotificationsList
              notifications={notifications}
              onChanged={() => void refresh()}
              onError={setError}
            />
          )}
          {tab === 'incidents' && <IncidentsList incidents={incidents} properties={properties} />}
          {tab === 'properties' && <PropertiesList properties={properties} />}
        </section>
      )}
    </main>
  );
}
