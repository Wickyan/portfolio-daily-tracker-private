import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { TrendingUp, TrendingDown, RefreshCw } from 'lucide-react'
import { portfolioService } from '@/services'
import type { Position } from '@/types'

export default function PortfolioPanel() {
  const queryClient = useQueryClient()
  const { data: portfolio, isLoading } = useQuery({
    queryKey: ['portfolio'],
    queryFn: portfolioService.getLivePortfolio,
    refetchInterval: 60000,
    refetchOnWindowFocus: false,
  })

  const refreshMutation = useMutation({
    mutationFn: portfolioService.refresh,
    onSuccess: async () => {
      await queryClient.refetchQueries({ queryKey: ['portfolio'], type: 'active' })
    },
  })

  const formatMoney = (value: number) => {
    return new Intl.NumberFormat('zh-CN', {
      style: 'currency',
      currency: 'CNY',
      minimumFractionDigits: 2,
    }).format(value)
  }

  const formatCurrency = (value: number, currency: string) => {
    try {
      return new Intl.NumberFormat('zh-CN', {
        style: 'currency',
        currency,
        minimumFractionDigits: 2,
      }).format(value)
    } catch {
      return `${currency} ${value.toFixed(2)}`
    }
  }

  const formatPercent = (value: number) => `${value.toFixed(2)}%`

  const renderPosition = (position: Position) => {
    const isProfit = position.profit >= 0

    return (
      <div
        key={`${position.account}-${position.code}-${position.currency}`}
        className="flex items-center justify-between py-3 border-b border-slate-700 last:border-b-0"
      >
        <div className="min-w-0 flex-1 pr-4">
          <div className="flex items-center gap-2">
            <span className="truncate font-medium">{position.name}</span>
            <span className="shrink-0 text-sm text-slate-400">{position.code}</span>
          </div>
          <div className="mt-1 text-sm text-slate-400">
            {position.account && `${position.account} | `}
            {position.quantity} | 成本 {position.cost_price.toFixed(2)} {position.currency}
          </div>
          <div className="mt-2">
            <div className="mb-1 flex items-center justify-between text-xs text-slate-400">
              <span>占总资产 {formatPercent(position.asset_weight_pct || 0)}</span>
              <span>占持仓 {formatPercent(position.holding_weight_pct || 0)}</span>
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-slate-700">
              <div
                className="h-full rounded-full bg-primary-500"
                style={{ width: `${Math.min(Math.max(position.asset_weight_pct || 0, 0), 100)}%` }}
              />
            </div>
          </div>
        </div>

        <div className="shrink-0 text-right">
          <div className="text-xs text-slate-400">总余额</div>
          <div className="font-semibold">{formatCurrency(position.market_value, position.currency)}</div>
          {position.market_value_cny !== null && position.market_value_cny !== undefined && position.currency !== 'CNY' && (
            <div className="text-xs text-slate-500">≈{formatMoney(position.market_value_cny)}</div>
          )}
          <div className="mt-1 text-xs text-slate-400">
            现价 {formatCurrency(position.current_price, position.currency)}
          </div>
          <div
            className={`mt-1 flex items-center justify-end gap-1 text-sm ${
              isProfit ? 'profit-positive' : 'profit-negative'
            }`}
          >
            {isProfit ? (
              <TrendingUp className="h-4 w-4" />
            ) : (
              <TrendingDown className="h-4 w-4" />
            )}
            <span>{formatCurrency(position.profit, position.currency)}</span>
            <span>({position.profit_pct >= 0 ? '+' : ''}{position.profit_pct.toFixed(2)}%)</span>
          </div>
        </div>
      </div>
    )
  }

  if (isLoading) {
    return (
      <div className="card">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">持仓</h2>
        </div>
        <div className="flex items-center justify-center py-8 text-slate-400">加载中...</div>
      </div>
    )
  }

  if (!portfolio || portfolio.positions.length === 0) {
    return (
      <div className="card">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">持仓</h2>
          <button
            onClick={() => refreshMutation.mutate()}
            className="p-2 text-slate-400 hover:text-white rounded-lg transition-colors"
          >
            <RefreshCw className={`h-4 w-4 ${refreshMutation.isPending ? 'animate-spin' : ''}`} />
          </button>
        </div>
        <div className="text-center py-8 text-slate-400">暂无持仓</div>
      </div>
    )
  }

  return (
    <div className="card">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-lg font-semibold">持仓</h2>
        <button
          onClick={() => refreshMutation.mutate()}
          disabled={refreshMutation.isPending}
          className="p-2 text-slate-400 hover:text-white rounded-lg transition-colors disabled:opacity-50"
        >
          <RefreshCw className={`h-4 w-4 ${refreshMutation.isPending ? 'animate-spin' : ''}`} />
        </button>
      </div>

      <div className="grid grid-cols-2 gap-4 mb-4 p-3 bg-slate-700/50 rounded-lg">
        <div>
          <div className="text-sm text-slate-400">持仓市值</div>
          <div className="font-semibold">{formatMoney(portfolio.total_market_value)}</div>
        </div>
        <div>
          <div className="text-sm text-slate-400">总盈亏</div>
          <div
            className={`font-semibold ${
              portfolio.total_profit >= 0 ? 'profit-positive' : 'profit-negative'
            }`}
          >
            {formatMoney(portfolio.total_profit)}
          </div>
        </div>
      </div>

      <div className="divide-y divide-slate-700">{portfolio.positions.map(renderPosition)}</div>
    </div>
  )
}
