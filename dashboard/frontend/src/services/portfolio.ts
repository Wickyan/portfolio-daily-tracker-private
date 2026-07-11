import api from './api'
import type { OperationSummary, PendingAction, Portfolio, PortfolioSummary } from '@/types'

export interface PositionIdentity {
  account: string
  code: string
  currency: string
}

export const portfolioService = {
  async getPortfolio(): Promise<Portfolio> {
    const response = await api.get<Portfolio>('/portfolio')
    return response.data
  },

  async getLivePortfolio(): Promise<Portfolio> {
    const response = await api.get<Portfolio>('/portfolio/live')
    return response.data
  },

  async getSummary(): Promise<PortfolioSummary> {
    const response = await api.get<PortfolioSummary>('/portfolio/summary')
    return response.data
  },

  async addPosition(data: {
    account: string
    code: string
    name: string
    currency: string
    asset_type: string
    quantity: number
    cost_price: number
    total_cost?: number
    fee?: number | null
    note?: string
  }) {
    const response = await api.post('/portfolio/add', data)
    return response.data
  },

  async updatePosition(
    identity: PositionIdentity,
    data: { quantity?: number; cost_price?: number },
  ) {
    const response = await api.put(`/portfolio/${encodeURIComponent(identity.code)}`, {
      account: identity.account,
      currency: identity.currency,
      ...data,
    })
    return response.data
  },

  async removePosition(identity: PositionIdentity) {
    const response = await api.delete(`/portfolio/${encodeURIComponent(identity.code)}`, {
      params: {
        account: identity.account,
        currency: identity.currency,
      },
    })
    return response.data
  },

  async refresh() {
    const response = await api.post('/portfolio/refresh')
    return response.data
  },

  async aiPreview(message: string, inputType: 'text' | 'image_text' = 'text'): Promise<PendingAction> {
    const response = await api.post<PendingAction>('/portfolio/ai-preview', {
      input_type: inputType,
      message,
    })
    return response.data
  },

  async aiRevise(pendingId: string, message: string): Promise<PendingAction> {
    const response = await api.post<PendingAction>('/portfolio/ai-revise', {
      pending_id: pendingId,
      message,
    })
    return response.data
  },

  async aiConfirm(pendingId: string) {
    const response = await api.post('/portfolio/ai-confirm', { pending_id: pendingId })
    return response.data
  },

  async aiReviseItem(pendingId: string, itemId: string, message: string) {
    const response = await api.post('/portfolio/ai-revise-item', {
      pending_id: pendingId,
      item_id: itemId,
      message,
    })
    return response.data
  },

  async aiConfirmItem(pendingId: string, itemId: string) {
    const response = await api.post('/portfolio/ai-confirm-item', {
      pending_id: pendingId,
      item_id: itemId,
    })
    return response.data
  },

  async aiRollbackItem(pendingId: string, itemId: string) {
    const response = await api.post('/portfolio/ai-rollback-item', {
      pending_id: pendingId,
      item_id: itemId,
    })
    return response.data
  },

  async aiCancel(pendingId: string) {
    const response = await api.post('/portfolio/ai-cancel', { pending_id: pendingId })
    return response.data
  },

  async getPending(pendingId: string): Promise<PendingAction> {
    const response = await api.get<PendingAction>(`/portfolio/pending/${encodeURIComponent(pendingId)}`)
    return response.data
  },

  async getOperations(): Promise<OperationSummary[]> {
    const response = await api.get<{ operations: OperationSummary[] }>('/portfolio/operations')
    return response.data.operations
  },

  async rollback(operationId: string) {
    const response = await api.post(`/portfolio/rollback/${operationId}`)
    return response.data
  },

  async rollbackLatest(): Promise<{
    ok: boolean
    rolled_back_operation_id: string
    rollback_operation_id: string
    summary?: string
    description?: string
  }> {
    const response = await api.post('/portfolio/rollback-latest')
    return response.data
  },
}
