import api from './api'
import type { OperationSummary, PendingAction, Portfolio, PortfolioSummary } from '@/types'

export const portfolioService = {
  // 获取持仓
  async getPortfolio(): Promise<Portfolio> {
    const response = await api.get<Portfolio>('/portfolio')
    return response.data
  },

  // 获取实时持仓（先刷新行情再返回）
  async getLivePortfolio(): Promise<Portfolio> {
    const response = await api.get<Portfolio>('/portfolio/live')
    return response.data
  },

  // 获取持仓摘要
  async getSummary(): Promise<PortfolioSummary> {
    const response = await api.get<PortfolioSummary>('/portfolio/summary')
    return response.data
  },

  // 添加持仓
  async addPosition(data: {
    account?: string
    group?: string
    code?: string
    symbol?: string
    name: string
    currency?: string
    asset_type?: string
    quantity: number
    cost_price: number
    fee?: number | null
    note?: string
  }) {
    const response = await api.post('/portfolio/add', data)
    return response.data
  },

  // 更新持仓
  async updatePosition(symbol: string, data: { quantity?: number; cost_price?: number }) {
    const response = await api.put(`/portfolio/${symbol}`, data)
    return response.data
  },

  // 删除持仓
  async removePosition(symbol: string) {
    const response = await api.delete(`/portfolio/${symbol}`)
    return response.data
  },

  // 刷新持仓价格
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

  async aiCancel(pendingId: string) {
    const response = await api.post('/portfolio/ai-cancel', { pending_id: pendingId })
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
}
