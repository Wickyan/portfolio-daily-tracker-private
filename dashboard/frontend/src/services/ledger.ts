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
  interpretation?: {
    warnings: string[]
    clauses: LedgerClause[]
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
