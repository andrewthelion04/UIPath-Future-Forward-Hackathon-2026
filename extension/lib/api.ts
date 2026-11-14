// Clientul API-ului REST StayPilot. Modelele de mai jos oglindesc modelele Pydantic din
// app/core/contracts/models.py: aceleași răspunsuri și aceleași ID-uri interne ca în Streamlit.
// Nimic din logica de business, din agenți sau din secrete nu ajunge în extensie.

// Fără `/` la final: altfel `${API_URL}/properties` devine `//properties` (404).
export const API_URL: string = (
  (import.meta.env.WXT_API_URL as string | undefined) || 'http://127.0.0.1:8000'
).replace(/\/+$/, '');

// Tokenul opțional al API-ului (`API_TOKEN` în backend), dat la build prin WXT_API_TOKEN.
const API_TOKEN = ((import.meta.env.WXT_API_TOKEN as string | undefined) ?? '').trim();
const AUTH_HEADERS: Record<string, string> = API_TOKEN ? { Authorization: `Bearer ${API_TOKEN}` } : {};

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
  }
}

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
  listing_url: string | null;
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
  status: 'pending' | 'approved' | 'rejected' | 'withdrawn';
  chosen_option: string | null;
  booking_id: string | null;
  property_id: string | null;
  created_at: string;
}

export type IncidentStatus =
  | 'awaiting_staff'
  | 'awaiting_decision'
  | 'awaiting_guest'
  | 'remediation_scheduled'
  | 'relocated'
  | 'dismissed'
  | 'resolved';

export interface StaffTask {
  id: string;
  description: string;
  status: 'open' | 'done';
  staff_name: string | null;
  completion_note: string | null;
  completed_at: string | null;
}

export interface Incident {
  id: string;
  property_id: string;
  booking_id: string | null;
  description: string;
  reporter: string;
  reported_via: string;
  severity: string;
  status: IncidentStatus;
  report_count: number;
  relocation_property_id: string | null;
  eta_hours: number | null;
  eta_source: string | null;
  recommendation: string | null;
  recommendation_reason: string | null;
  guest_answer: string | null;
  resolution: string | null;
  tasks: StaffTask[];
  updated_at: string;
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
      headers: { 'Content-Type': 'application/json', ...AUTH_HEADERS, ...init?.headers },
    });
  } catch {
    throw new ApiError(`Cannot reach the StayPilot API at ${API_URL}. Is it running?`);
  }
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as { detail?: unknown } | null;
    let detail = typeof body?.detail === 'string' ? body.detail : `HTTP ${response.status}`;
    if (response.status === 401) detail += '. Rebuild the extension with WXT_API_TOKEN set to the API token.';
    throw new ApiError(detail, response.status);
  }
  return (await response.json()) as T;
}

export const api = {
  properties: () => request<Property[]>('/properties'),
  notifications: () => request<Notification[]>('/notifications'),
  approvals: () => request<Approval[]>('/approvals'),
  // Incidentele aparțin unui feature opțional: dacă e dezactivat, endpointul nu există (404)
  // și panoul arată o listă goală, fără să piardă aprobările și notificările.
  incidents: () =>
    request<Incident[]>('/incidents').catch((error: unknown) => {
      if (error instanceof ApiError && error.status === 404) return [];
      throw error;
    }),
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
