import api from './api'

export interface LedgerEvent {
  transaction_id: string
  event_type: string
  effective_at: string
  entered_at: string
  account: string
  instrument_id?: string | null
  code?: string | null
  name?: string | null
  currency?: string | null
  quantity?: string | null
  price?: string | null
  fee?: string | null
  tax?: string | null
  amount?: string | null
  counter_currency?: string | null
  counter_amount?: string | null
  source: string
  note: string
  external_trade_id?: string | null
  reverses_transaction_id?: string | null
  is_reversed?: boolean
  reversed_by?: string | null
  metadata?: Record<string, unknown>
}

export interface LedgerPosition {
  account: string
  code: string
  name: string
  currency: string
  quantity: string
  total_cost: string
  average_cost: string
  realized_pnl: string
  dividend_income: string
}

export interface LedgerCash {
  account: string
  currency: string
  amount: string
}

export interface LedgerState {
  as_of?: string | null
  positions: LedgerPosition[]
  cash_accounts: LedgerCash[]
  applied_transactions: number
  reversed_transaction_ids?: string[]
}

export interface LedgerClause {
  original: string
  cleaned: string
  effective_at: string
  date_precision: string
  action_type: string
  warnings: string[]
}

export interface LedgerPreview {
  pending_id: string
  status: string
  created_at: string
  expires_at: string
  historical_backfill: boolean
  events: LedgerEvent[]
  projected_state: LedgerState
  confirm_blocked?: boolean
  replay_error?: string | null
  interpretation?: {
    warnings: string[]
    clauses: LedgerClause[]
  }
}

export interface ScreenshotExtractionCandidate {
  event?: LedgerEvent | null
  raw: Record<string, unknown>
  warnings: string[]
  missing_fields: string[]
  duplicate_reason?: string | null
}

export interface ScreenshotImageSummary {
  filename: string
  sha256: string
  width: number
  height: number
  tile_count?: number
  raw_rows?: number
  kind?: string
  ocr_rows?: number
}

export interface ReconciliationPositionSnapshot {
  code?: string | null
  reported_code?: string | null
  name?: string | null
  quantity?: string | null
  average_cost?: string | null
  current_price?: string | null
  market_value?: string | null
  currency?: string | null
  unrealized_pnl?: string | null
  confidence?: number | null
  inferred_fields?: string[]
  warnings?: string[]
}

export interface ReconciliationCashSnapshot {
  currency: string
  amount: string
  label?: string
  confidence?: number | null
}

export interface ReconciliationSnapshot {
  snapshot_id?: string
  captured_at: string
  entered_at?: string
  account: string
  source: string
  source_hash: string
  filename: string
  model_label: string
  positions: ReconciliationPositionSnapshot[]
  cash: ReconciliationCashSnapshot[]
  metadata: Record<string, unknown>
  warnings?: string[]
}

export interface ReconciliationComparisonPosition {
  account: string
  code: string
  currency: string
  name?: string
  status: string
  snapshot_quantity?: string | null
  ledger_quantity?: string | null
  quantity_difference?: string | null
  snapshot_average_cost?: string | null
  ledger_average_cost?: string | null
  cost_difference?: string | null
}

export interface ReconciliationComparison {
  status: string
  summary: { matches: number; differences: number }
  positions: ReconciliationComparisonPosition[]
  cash: Array<Record<string, string | null>>
  replay_error?: string | null
}

export interface ReconciliationPreviewItem {
  snapshot: ReconciliationSnapshot
  comparison: ReconciliationComparison
}

export interface LedgerScreenshotPreview {
  image_kind?: 'trade_history' | 'holdings_snapshot'
  pending_id: string | null
  status: string
  created_at?: string | null
  expires_at?: string | null
  historical_backfill: boolean
  events: LedgerEvent[]
  projected_state: LedgerState
  confirm_blocked?: boolean
  replay_error?: string | null
  reconciliation?: { snapshots: ReconciliationPreviewItem[] }
  extraction: {
    model: string
    images: ScreenshotImageSummary[]
    warnings: string[]
    duplicate_count: number
    duplicates: ScreenshotExtractionCandidate[]
    unresolved_count: number
    unresolved: ScreenshotExtractionCandidate[]
  }
}

export interface LedgerTransactionsResponse {
  count: number
  transactions: LedgerEvent[]
}

export interface LedgerConfirmResponse {
  pending_id: string
  status: string
  historical_backfill: boolean
  events: LedgerEvent[]
  projected_state: LedgerState
}

function localIsoWithOffset(date = new Date()): string {
  const pad = (value: number) => String(value).padStart(2, '0')
  const offsetMinutes = -date.getTimezoneOffset()
  const sign = offsetMinutes >= 0 ? '+' : '-'
  const abs = Math.abs(offsetMinutes)
  const offsetHours = Math.floor(abs / 60)
  const offsetMins = abs % 60
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}` +
    `${sign}${pad(offsetHours)}:${pad(offsetMins)}`
  )
}

export const ledgerService = {
  localReferenceTime: localIsoWithOffset,

  async getTransactions(params?: {
    account?: string
    code?: string
    from?: string
    to?: string
  }): Promise<LedgerTransactionsResponse> {
    const response = await api.get<LedgerTransactionsResponse>('/ledger-v3/transactions', {
      params,
    })
    return response.data
  },

  async getState(asOf?: string): Promise<LedgerState> {
    const response = await api.get<LedgerState>('/ledger-v3/state', {
      params: asOf ? { as_of: asOf } : undefined,
    })
    return response.data
  },

  async previewText(
    message: string,
    source: 'text' | 'voice' = 'text',
    referenceTime = localIsoWithOffset(),
  ): Promise<LedgerPreview> {
    const response = await api.post<LedgerPreview>(
      '/ledger-v3/preview-text',
      {
        message,
        source,
        reference_time: referenceTime,
      },
      { timeout: 30000 },
    )
    return response.data
  },

  async previewScreenshots(
    images: File[],
    accountHint = '',
    capturedAtHint = '',
  ): Promise<LedgerScreenshotPreview> {
    const form = new FormData()
    images.forEach((image) => form.append('images', image))
    if (accountHint.trim()) form.append('account_hint', accountHint.trim())
    if (capturedAtHint.trim()) form.append('captured_at_hint', capturedAtHint.trim())
    const response = await api.post<LedgerScreenshotPreview>(
      '/ledger-v3/preview-screenshots',
      form,
      { timeout: 180000 },
    )
    return response.data
  },

  async saveReconciliationSnapshot(snapshot: ReconciliationSnapshot): Promise<ReconciliationPreviewItem> {
    const response = await api.post<ReconciliationPreviewItem>(
      '/ledger-v3/reconciliation-snapshots',
      snapshot,
      { timeout: 30000 },
    )
    return response.data
  },

  async getReconciliationSnapshots(account?: string): Promise<{ count: number; snapshots: ReconciliationPreviewItem[] }> {
    const response = await api.get('/ledger-v3/reconciliation-snapshots', {
      params: account ? { account } : undefined,
    })
    return response.data
  },

  async previewReverse(
    transactionId: string,
    reason = '',
  ): Promise<LedgerPreview> {
    const response = await api.post<LedgerPreview>(
      `/ledger-v3/reverse/${encodeURIComponent(transactionId)}/preview`,
      { reason },
      { timeout: 30000 },
    )
    return response.data
  },

  async confirm(pendingId: string): Promise<LedgerConfirmResponse> {
    const response = await api.post<LedgerConfirmResponse>(
      `/ledger-v3/confirm/${encodeURIComponent(pendingId)}`,
      undefined,
      { timeout: 30000 },
    )
    return response.data
  },
}

export default ledgerService
