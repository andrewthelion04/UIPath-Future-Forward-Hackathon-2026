// Clientul API-ului REST StayPilot. Modelele de mai jos oglindesc modelele Pydantic din
// app/core/contracts/models.py: aceleași răspunsuri și aceleași ID-uri interne ca în Streamlit.
// Nimic din logica de business, din agenți sau din secrete nu ajunge în extensie.

export const API_URL: string =
  (import.meta.env.WXT_API_URL as string | undefined) ?? 'http://127.0.0.1:8000';

export interface Property {
  id: string;
  name: string;
  city: string;
  address: string;
  bedrooms: number;
  max_guests: number;
  amenities: string[];
  base_price: number;
  currency: string;
  pms_provider: string;
}

export type NotificationLevel = 'info' | 'success' | 'warning' | 'error' | 'action_required';

export interface Notification {
  id: string;
  feature: string;
  level: NotificationLevel;
  title: string;
  body: string;
  booking_id: string | null;
  property_id: string | null;
  approval_id: string | null;
  read: boolean;
  created_at: string;
}

export interface ApprovalOption {
  key: string;
  label: string;
  description: string;
}

export interface Approval {
  id: string;
  feature: string;
  kind: string;
  title: string;
  summary: string;
  options: ApprovalOption[];
  status: 'pending' | 'approved' | 'rejected';
  chosen_option: string | null;
  booking_id: string | null;
  property_id: string | null;
  created_at: string;
}

export interface DecisionResult {
  approval: Approval;
  applied: boolean;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...init?.headers },
    });
  } catch {
    throw new Error(`Cannot reach the StayPilot API at ${API_URL}. Is it running?`);
  }
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as { detail?: unknown } | null;
    const detail = typeof body?.detail === 'string' ? body.detail : `HTTP ${response.status}`;
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

export const api = {
  properties: () => request<Property[]>('/properties'),
  notifications: () => request<Notification[]>('/notifications'),
  approvals: () => request<Approval[]>('/approvals'),
  decide: (approvalId: string, decision: 'approve' | 'reject', option?: string) =>
    request<DecisionResult>(`/approvals/${approvalId}/decision`, {
      method: 'POST',
      body: JSON.stringify({ decision, option: option ?? null }),
    }),
  markRead: (notificationId: string) =>
    request<Notification>(`/notifications/${notificationId}/read`, { method: 'POST' }),
};

export function formatTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}
